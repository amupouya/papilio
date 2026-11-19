"""PostgreSQL ownership and native public-ID serialization contracts."""

import inspect

import pytest
from alembic.autogenerate import render_python_code
from alembic.migration import MigrationContext
from alembic.operations.ops import CreateTableOp, UpgradeOps
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import CompileError
from sqlalchemy.schema import CreateTable

from papilio.api.application import create_app
from papilio.infra.db.repositories.backends.postgresql import (
    PGPublicPersistenceRepository,
)
from papilio.infra.db.schema.postgresql import (
    PGPublicIdentifiedEntity,
    PGPublicIDField,
    PGPublicPersistenceEntity,
)
from papilio.schemas.outputs import PublicIDOutput


class PublicItem(PGPublicIdentifiedEntity, table=True):
    __tablename__ = "public_id_unit_items"
    title: str


class RangedItem(PGPublicPersistenceEntity, table=True):
    __tablename__ = "public_id_unit_ranges"
    public_id: int = PGPublicIDField(start=2_000_000_000, end=2_999_999_999)


def test_generation_belongs_only_to_postgresql():
    ddl = str(
        CreateTable(PublicItem.__table__).compile(dialect=postgresql.dialect())
    )
    assert "GENERATED ALWAYS AS" in ddl
    assert "range_encrypt_element" in ddl
    assert "STORED" in ddl
    assert "UNIQUE (public_id)" in ddl
    assert "9000000000" in ddl
    with pytest.raises(CompileError, match="PostgreSQL"):
        CreateTable(PublicItem.__table__).compile(dialect=sqlite.dialect())


@pytest.mark.parametrize(
    "options",
    [
        {"digits": 0},
        {"digits": 19},
        {"digits": True},
        {"start": 10},
        {"end": 20},
        {"start": 20, "end": 10},
        {"start": 0, "end": 10},
        {"start": 1, "end": 3},
        {"start": 1, "end": 2**63},
        {"digits": 10, "start": 1, "end": 100},
    ],
)
def test_invalid_ranges_fail_at_declaration(options):
    with pytest.raises(ValueError):
        PGPublicIDField(**options)


def test_generated_identity_is_excluded_from_writes():
    item = PublicItem(id=42, public_id=1_234_567_890, title="Stored")
    assert item.to_row() == {"id": 42, "title": "Stored"}
    assert item.to_row(exclude_unset=False) == {"id": 42, "title": "Stored"}
    assert item.to_changes() == {"title": "Stored"}
    assert item.to_dict()["public_id"] == 1_234_567_890


def test_output_uses_public_value_without_falling_back_to_internal_id():
    item = PublicItem(id=42, public_id=1_234_567_890, title="Stored")
    assert PublicIDOutput.from_obj(item).model_dump() == {"id": 1_234_567_890}
    assert PublicIDOutput.model_validate({"public_id": 1234}).id == 1234
    with pytest.raises(ValidationError):
        PublicIDOutput.model_validate({"id": 42})
    assert (
        "id"
        in PublicIDOutput.model_json_schema(mode="serialization")["properties"]
    )


def test_ranged_and_timestamp_entity_is_independent():
    ddl = str(
        CreateTable(RangedItem.__table__).compile(dialect=postgresql.dialect())
    )
    assert "2000000000" in ddl
    assert "1000000000" in ddl
    assert "created_at" in ddl
    assert (
        PublicItem.__table__.c.public_id
        is not RangedItem.__table__.c.public_id
    )


def test_native_alembic_renders_a_plain_computed_expression():
    context = MigrationContext.configure(dialect=postgresql.dialect())
    migration = render_python_code(
        UpgradeOps([CreateTableOp.from_table(PublicItem.__table__)]),
        migration_context=context,
    )
    assert "sa.Computed(" in migration
    assert "range_encrypt_element" in migration
    assert "persisted=True" in migration
    assert "_PublicIDExpression" not in migration


def test_public_persistence_reuses_native_timestamp_ordering():
    class Repo(PGPublicPersistenceRepository[RangedItem]):
        table = RangedItem

    repo = Repo(None)
    stmt = repo._time_query().compile(dialect=postgresql.dialect())
    assert str(stmt).endswith(
        "ORDER BY public_id_unit_ranges.created_at, public_id_unit_ranges.id"
    )


def test_application_has_no_encryption_binding():
    assert "id_encryptions" not in inspect.signature(create_app).parameters
