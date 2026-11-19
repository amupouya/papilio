"""Real PostgreSQL public-ID generation and repository operations."""

import asyncio
import os

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import Field
from sqlalchemy import event, select, text
from sqlalchemy.exc import DBAPIError

from papilio.api.responses.envelope import APIResponse
from papilio.api.responses.handlers import setup_exception_handlers
from papilio.core import resources
from papilio.errors.exceptions import NotFoundException
from papilio.infra.db.connection import DBConnection
from papilio.infra.db.repositories.backends.postgresql import (
    PGPublicIdentifiedRepository,
)
from papilio.infra.db.schema.fields import ForeignKeyField
from papilio.infra.db.schema.postgresql import (
    PGPublicIdentifiedEntity,
    PGPublicIDField,
)
from papilio.infra.db.transaction import transaction
from papilio.infra.db.uow import PGUnitOfWork
from papilio.schemas.outputs import PublicIDOutput
from papilio.types.aliases import IdType


class PublicRecord(PGPublicIdentifiedEntity, table=True):
    __tablename__ = "papilio_public_id_records"
    public_id: int = PGPublicIDField(start=1_000_000, end=1_001_999)
    title: str
    parent_id: int | None = ForeignKeyField(
        "papilio_public_id_records.id", default=None, nullable=True
    )


class PublicRecordRepository(PGPublicIdentifiedRepository[PublicRecord]):
    table = PublicRecord


@pytest.fixture
async def public_db():
    dsn = os.environ.get("PAPILIO_TEST_POSTGRESQL")
    if not dsn:
        pytest.skip("Set PAPILIO_TEST_POSTGRESQL for live public-ID tests")
    db = DBConnection(dsn, 8, 0, 5, 1800, uow_factory=PGUnitOfWork)
    async with db.engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS permuteseq"))
        await conn.run_sync(PublicRecord.__table__.create)
    try:
        yield db
    finally:
        async with db.engine.begin() as conn:
            await conn.run_sync(PublicRecord.__table__.drop)
        await db.dispose()


async def test_single_and_bulk_generation_return_stored_values(public_db):
    statements = []
    event.listen(
        public_db.engine.sync_engine,
        "before_cursor_execute",
        lambda conn, cursor, stmt, params, context, many: statements.append(
            stmt
        ),
    )
    async with public_db.uow() as unit, transaction():
        repo = PublicRecordRepository(unit)
        first = await repo.create(PublicRecord(title="First"))
        assert len(statements) == 1
        assert "RETURNING" in statements[0]
        others = await repo.bulk_create(
            [PublicRecord(title=str(i)) for i in range(999)]
        )
        records = [first, *others]
        public_ids = [item.public_id for item in records]
        assert len(set(public_ids)) == len(records)
        assert all(1_000_000 <= value <= 1_001_999 for value in public_ids)
        assert public_ids != sorted(public_ids)
        saved = first.public_id
        assert (await repo.get_by_id(first.id)).public_id == saved
        assert (await repo.get_by_public_id(saved)).id == first.id
        assert PublicIDOutput.from_obj(first).id == saved
    async with public_db.uow() as unit:
        assert (
            await PublicRecordRepository(unit).get_by_public_id(saved)
        ).public_id == saved


async def test_concurrent_committed_inserts_never_repeat(public_db):
    async def create(index):
        async with public_db.uow() as unit, transaction():
            record = await PublicRecordRepository(unit).create(
                PublicRecord(title=str(index))
            )
            return record.public_id

    public_ids = await asyncio.gather(*(create(i) for i in range(48)))
    assert len(set(public_ids)) == 48
    async with public_db.uow() as unit:
        assert await PublicRecordRepository(unit).count() == 48


async def test_public_operations_preserve_internal_operations(public_db):
    async with public_db.uow() as unit, transaction():
        repo = PublicRecordRepository(unit)
        first, second = await repo.bulk_create(
            [PublicRecord(title="One"), PublicRecord(title="Two")]
        )
        assert await repo.get_by_public_id(99) is None
        assert {
            item.id
            for item in await repo.get_by_public_ids(
                [first.public_id, second.public_id]
            )
        } == {first.id, second.id}
        updated = await repo.update_by_public_id(
            first.public_id, {"title": "Updated"}
        )
        assert updated.id == first.id
        assert updated.public_id == first.public_id
        assert updated.title == "Updated"
        assert await repo.update_by_public_id(99, {"title": "Missing"}) is None
        assert (
            len(
                await repo.update_by_public_ids(
                    [first.public_id, second.public_id], {"title": "Both"}
                )
            )
            == 2
        )
        assert (await repo.remove_by_public_id(first.public_id)).id == first.id
        assert await repo.remove_by_public_id(first.public_id) is None
        assert len(await repo.remove_by_public_ids([second.public_id])) == 1
        assert await repo.get_by_public_ids([]) == []


async def test_loaded_rows_and_bulk_updates_leave_generation_to_db(public_db):
    async with public_db.uow() as unit, transaction():
        repo = PublicRecordRepository(unit)
        row = await repo.create(PublicRecord(title="Initial"))
        public_id = row.public_id
        row.title = "Upserted"
        columns = PublicRecord.__table__.c
        saved = await repo.upsert(
            row, conflict_columns=[columns.id], update_columns=[columns.title]
        )
        assert saved.public_id == public_id
        await repo.bulk_upsert(
            [saved],
            insert_columns={"id": columns.id, "title": columns.title},
            conflict_columns=[columns.id],
            update_columns=[columns.title],
        )
        rows = await repo.bulk_update(
            [{"id": saved.id, "title": "Bulk updated"}],
            key_columns=[columns.id],
            update_columns=[columns.title],
        )
        assert rows[0].public_id == public_id
        assert rows[0].title == "Bulk updated"


async def test_http_foreign_reference_and_errors_use_stored_public_ids(
    public_db,
):
    async with public_db.uow() as unit, transaction():
        repo = PublicRecordRepository(unit)
        parent = await repo.create(PublicRecord(title="Parent"))
        child = await repo.create(
            PublicRecord(title="Child", parent_id=parent.id)
        )
        parent_public_id, child_public_id = parent.public_id, child.public_id

    class Output(PublicIDOutput):
        parent_id: int | None = Field(validation_alias="parent_public_id")

    app = FastAPI()
    setup_exception_handlers(app)

    @app.get("/{id}", response_model=APIResponse[Output, None])
    async def read(id: IdType):
        table = PublicRecord.__table__
        related = table.alias("parent")
        stmt = select(
            table.c.public_id,
            related.c.public_id.label("parent_public_id"),
        ).select_from(
            table.outerjoin(related, table.c.parent_id == related.c.id)
        )
        stmt = stmt.where(table.c.public_id == id)
        async with public_db.uow() as unit:
            result = await unit.execute(stmt)
            row = result.mappings().one_or_none()
        if row is None:
            raise NotFoundException(
                message=f"Missing record {id}",
                message_code=resources.NOT_FOUND_ERROR,
                entity="Record",
                identifier="id",
                identifier_value=id,
            )
        return APIResponse.from_data(Output.from_dict(row))

    async with AsyncClient(
        transport=ASGITransport(app), base_url="http://test"
    ) as client:
        assert (await client.get(f"/{child_public_id}")).json()["data"] == {
            "id": child_public_id,
            "parent_id": parent_public_id,
        }
        assert (await client.get(f"/{parent_public_id}")).json()["data"] == {
            "id": parent_public_id,
            "parent_id": None,
        }
        missing = await client.get("/9999999")
        assert missing.status_code == 404
        assert missing.json()["error"]["identifier_value"] == "9999999"


async def test_generated_id_is_immutable_and_capacity_fails_without_reuse(
    public_db,
):
    async with public_db.uow() as unit, transaction():
        repo = PublicRecordRepository(unit)
        records = await repo.bulk_create(
            [PublicRecord(title=str(i)) for i in range(2000)]
        )
        assert len({record.public_id for record in records}) == 2000
    with pytest.raises(DBAPIError, match="outside of range"):
        async with public_db.uow() as unit, transaction():
            await PublicRecordRepository(unit).create(
                PublicRecord(title="Full")
            )
    with pytest.raises(DBAPIError, match="generated column"):
        async with public_db.uow() as unit, transaction():
            await PublicRecordRepository(unit).update_by_id(
                1, {"public_id": 1_000_000}
            )
    async with public_db.uow() as unit:
        assert await PublicRecordRepository(unit).count() == 2000
        assert (
            len((await unit.execute(select(PublicRecord.public_id))).all())
            == 2000
        )
