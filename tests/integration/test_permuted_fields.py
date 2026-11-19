"""Real PostgreSQL defaults with the ordinary repository/output families."""

import asyncio
import os

import pytest
import sqlalchemy as sa
from alembic.autogenerate import render_python_code
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.operations.ops import CreateTableOp, UpgradeOps
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError

from papilio.api.responses.envelope import APIResponse
from papilio.api.responses.handlers import setup_exception_handlers
from papilio.core import resources
from papilio.errors.exceptions import NotFoundException
from papilio.infra.db.connection import DBConnection
from papilio.infra.db.repositories.backends.postgresql import (
    PGPersistenceRepository,
    PGRepository,
)
from papilio.infra.db.schema.entity import (
    BaseEntity,
    IdentifiedEntity,
    PersistenceEntity,
)
from papilio.infra.db.schema.fields import ForeignKeyField
from papilio.infra.db.schema.postgresql import PermutedBigIntField
from papilio.infra.db.table import BaseTable
from papilio.infra.db.transaction import transaction
from papilio.infra.db.uow import PGUnitOfWork
from papilio.schemas.outputs import BaseOutput
from papilio.types.aliases import IdType


class RecordModel(PersistenceEntity):
    id: int = PermutedBigIntField(
        start=1_000_000, end=1_001_999, primary_key=True
    )
    title: str
    parent_id: int | None = ForeignKeyField(
        "permuted_records.id", default=None, nullable=True
    )


class Record(RecordModel, BaseTable, table=True):
    table_name = "permuted_records"


class RecordRepository(PGPersistenceRepository[RecordModel]):
    table = Record


class Codes(BaseEntity, table=True):
    __tablename__ = "permuted_codes"
    key: int = PermutedBigIntField(digits=7, primary_key=True)
    code: int = PermutedBigIntField(digits=8, unique=True)
    number: int = PermutedBigIntField(digits=6)


class CodeRepository(PGRepository[Codes]):
    table = Codes


@pytest.fixture
async def database():
    dsn = os.environ.get("PAPILIO_TEST_POSTGRESQL")
    if not dsn:
        pytest.skip(
            "Set PAPILIO_TEST_POSTGRESQL for live permuted-field tests"
        )
    db = DBConnection(dsn, 8, 0, 5, 1800, uow_factory=PGUnitOfWork)
    async with db.engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS permuteseq"))
        for table in (Record.__table__, Codes.__table__):
            await conn.run_sync(table.create)
    try:
        yield db
    finally:
        async with db.engine.begin() as conn:
            for table in (Codes.__table__, Record.__table__):
                await conn.run_sync(table.drop)
            assert (
                await conn.scalar(
                    text("SELECT to_regclass('permuted_records_id_seq')")
                )
                is None
            )
        await db.dispose()


async def test_single_and_bulk_generation_return_stored_values(database):
    statements = []
    event.listen(
        database.engine.sync_engine,
        "before_cursor_execute",
        lambda conn, cursor, stmt, params, context, many: statements.append(
            stmt
        ),
    )
    async with database.uow() as unit, transaction():
        repo = RecordRepository(unit)
        first = await repo.create(RecordModel(title="First"))
        assert len(statements) == 1 and "RETURNING" in statements[0]
        before = len(statements)
        others = await repo.bulk_create(
            [RecordModel(title=str(i)) for i in range(999)]
        )
        assert len(statements) == before + 1
        ids = [item.id for item in [first, *others]]
        assert len(set(ids)) == 1000
        assert all(1_000_000 <= value <= 1_001_999 for value in ids)
        assert ids != sorted(ids)
        saved = first.id
    async with database.uow() as unit:
        assert (await RecordRepository(unit).get_by_id(saved)).id == saved
    await database.engine.dispose()
    async with database.uow() as unit:
        assert (await RecordRepository(unit).get_by_id(saved)).id == saved


async def test_concurrent_committed_inserts_never_repeat(database):
    async def create(index):
        async with database.uow() as unit, transaction():
            return (
                await RecordRepository(unit).create(
                    RecordModel(title=str(index))
                )
            ).id

    ids = await asyncio.gather(*(create(i) for i in range(48)))
    assert len(set(ids)) == 48
    async with database.uow() as unit:
        assert await RecordRepository(unit).count() == 48


async def test_existing_single_and_set_repository_operations(database):
    async with database.uow() as unit, transaction():
        repo = RecordRepository(unit)
        first, second = await repo.bulk_create(
            [RecordModel(title="One"), RecordModel(title="Two")]
        )
        assert await repo.get_by_id(99) is None
        assert {
            row.id for row in await repo.get_by_ids([first.id, second.id])
        } == {first.id, second.id}
        updated = await repo.update_by_id(first.id, {"title": "Updated"})
        assert updated.id == first.id and updated.title == "Updated"
        assert await repo.update_by_id(99, {"title": "Missing"}) is None
        assert (
            len(
                await repo.update_by_ids(
                    [first.id, second.id], {"title": "Both"}
                )
            )
            == 2
        )
        assert (await repo.remove_by_id(first.id)).id == first.id
        assert await repo.remove_by_id(first.id) is None
        assert len(await repo.remove_by_ids([second.id])) == 1
        assert await repo.get_by_ids([]) == []


async def test_loaded_upsert_and_batch_update_preserve_the_id(database):
    async with database.uow() as unit, transaction():
        repo = RecordRepository(unit)
        row = await repo.create(RecordModel(title="Initial"))
        saved_id = row.id
        row.title = "Upserted"
        columns = Record.__table__.c
        saved = await repo.upsert(
            row, conflict_columns=[columns.id], update_columns=[columns.title]
        )
        assert saved.id == saved_id
        await repo.bulk_upsert(
            [saved],
            insert_columns={"id": columns.id, "title": columns.title},
            conflict_columns=[columns.id],
            update_columns=[columns.title],
        )
        rows = await repo.bulk_update(
            [{"id": saved_id, "title": "Bulk"}],
            key_columns=[columns.id],
            update_columns=[columns.title],
        )
        assert rows[0].id == saved_id and rows[0].title == "Bulk"
        assert await repo.count() == 1


async def test_plain_http_output_and_foreign_references_use_the_same_id(
    database,
):
    async with database.uow() as unit, transaction():
        repo = RecordRepository(unit)
        parent = await repo.create(RecordModel(title="Parent"))
        child = await repo.create(
            RecordModel(title="Child", parent_id=parent.id)
        )
        parent_id, child_id = parent.id, child.id

    class Output(BaseOutput):
        id: int
        parent_id: int | None

    app = FastAPI()
    setup_exception_handlers(app)

    @app.get("/{id}", response_model=APIResponse[Output, None])
    async def read(id: IdType):
        async with database.uow() as unit:
            row = await RecordRepository(unit).get_by_id(id)
            if row is not None:
                return APIResponse.from_data(Output.from_obj(row))
        raise NotFoundException(
            message=f"Missing record {id}",
            message_code=resources.NOT_FOUND_ERROR,
            entity="Record",
            identifier="id",
            identifier_value=id,
        )

    async with AsyncClient(
        transport=ASGITransport(app), base_url="http://test"
    ) as client:
        assert (await client.get(f"/{child_id}")).json()["data"] == {
            "id": child_id,
            "parent_id": parent_id,
        }
        assert (await client.get(f"/{parent_id}")).json()["data"] == {
            "id": parent_id,
            "parent_id": None,
        }
        assert (await client.get("/9999999")).json()["error"][
            "identifier_value"
        ] == "9999999"


async def test_capacity_exhaustion_and_rollback_never_reuse_positions(
    database,
):
    with pytest.raises(ValueError, match="rollback"):
        async with database.uow() as unit, transaction():
            await RecordRepository(unit).create(
                RecordModel(title="Rolled back")
            )
            raise ValueError("rollback")
    async with database.uow() as unit, transaction():
        rows = await RecordRepository(unit).bulk_create(
            [RecordModel(title=str(i)) for i in range(1999)]
        )
        assert len({row.id for row in rows}) == 1999
    with pytest.raises(DBAPIError, match="maximum value"):
        async with database.uow() as unit, transaction():
            await RecordRepository(unit).create(RecordModel(title="Full"))
    async with database.uow() as unit:
        assert await RecordRepository(unit).count() == 1999


async def test_non_primary_fields_generate_independently_on_base_entity(
    database,
):
    async with database.uow() as unit, transaction():
        rows = await CodeRepository(unit).bulk_create(
            [Codes() for _ in range(20)]
        )
        assert len({row.key for row in rows}) == 20
        assert all(1_000_000 <= row.key <= 9_999_999 for row in rows)
        assert len({row.code for row in rows}) == 20
        assert all(10_000_000 <= row.code <= 99_999_999 for row in rows)
        assert all(100_000 <= row.number <= 999_999 for row in rows)
        assert not Codes.__table__.c.number.unique
        # Explicit values use native DEFAULT behavior and selected constraints.
        duplicate = await CodeRepository(unit).create(
            Codes(number=rows[0].number)
        )
        assert duplicate.number == rows[0].number


async def test_native_alembic_upgrade_downgrade_and_sequence_ownership(
    database,
):
    class Migrated(IdentifiedEntity, table=True):
        __tablename__ = "permuted_migration"
        id: int = PermutedBigIntField(start=100, end=999, primary_key=True)

    rendered = render_python_code(
        UpgradeOps([CreateTableOp.from_table(Migrated.__table__)]),
        migration_context=MigrationContext.configure(
            dialect=sa.dialects.postgresql.dialect()
        ),
    )
    assert "sa.text(" in rendered and "_PermutedDefault" not in rendered

    def upgrade(connection):
        op = Operations(MigrationContext.configure(connection))
        op.execute(
            sa.schema.CreateSequence(
                sa.Sequence(
                    "permuted_migration_id_seq",
                    start=100,
                    minvalue=100,
                    maxvalue=999,
                    cycle=False,
                )
            )
        )
        namespace = {"op": op, "sa": sa}
        exec("def run():\n" + rendered, namespace)
        namespace["run"]()
        op.execute(
            "ALTER SEQUENCE permuted_migration_id_seq "
            "OWNED BY permuted_migration.id"
        )

    async with database.engine.begin() as connection:
        await connection.run_sync(upgrade)
        values = (
            (
                await connection.execute(
                    text(
                        "INSERT INTO permuted_migration "
                        "SELECT FROM generate_series(1,20) RETURNING id"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(set(values)) == 20 and all(
            100 <= value <= 999 for value in values
        )
        await connection.run_sync(
            lambda conn: Operations(
                MigrationContext.configure(conn)
            ).drop_table("permuted_migration")
        )
        assert (
            await connection.scalar(
                text("SELECT to_regclass('permuted_migration_id_seq')")
            )
            is None
        )


async def test_quoted_schema_metadata_copy_and_external_drop(database):
    class Quoted(IdentifiedEntity, table=True):
        __tablename__ = "Quoted Table"
        __table_args__ = {"schema": "Mixed Schema"}
        id: int = PermutedBigIntField(digits=6, primary_key=True)

    table = Quoted.__table__.to_metadata(sa.MetaData())
    async with database.engine.begin() as connection:
        await connection.execute(text('CREATE SCHEMA "Mixed Schema"'))
        await connection.run_sync(table.create)
        value = await connection.scalar(table.insert().returning(table.c.id))
        assert 100_000 <= value <= 999_999
        await connection.execute(
            text('DROP TABLE "Mixed Schema"."Quoted Table"')
        )
        assert (
            await connection.scalar(
                text("SELECT to_regclass(:name)"),
                {"name": '"Mixed Schema"."Quoted Table_id_seq"'},
            )
            is None
        )
        await connection.execute(text('DROP SCHEMA "Mixed Schema"'))
