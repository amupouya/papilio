"""Independent PostgreSQL permuted fields and ordinary entity contracts."""

import pytest
from sqlalchemy import MetaData, create_mock_engine
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import CompileError
from sqlalchemy.schema import CreateTable

from papilio.infra.db.schema.entity import BaseEntity, PersistenceEntity
from papilio.infra.db.schema.fields import IDField
from papilio.infra.db.schema.postgresql import PermutedBigIntField


class Model(BaseEntity):
    key: int = PermutedBigIntField(digits=8, primary_key=True)
    code: int = PermutedBigIntField(start=100, end=999)


class First(Model, table=True):
    __tablename__ = "permuted_first"


class Second(Model, table=True):
    __tablename__ = "permuted_second"


class Record(PersistenceEntity, table=True):
    __tablename__ = "permuted_record"
    id: int = PermutedBigIntField(digits=10, primary_key=True)


def test_independent_fields_preserve_caller_selected_constraints():
    table = First.__table__
    assert list(table.primary_key.columns.keys()) == ["key"]
    assert not table.c.code.primary_key and not table.c.code.unique
    assert table.c.code.computed is None
    ddl = str(CreateTable(table).compile(dialect=postgresql.dialect()))
    assert "permute_nextval" in ddl and "BIGINT" in ddl
    assert "GENERATED ALWAYS" not in ddl and "UNIQUE" not in ddl
    with pytest.raises(CompileError, match="PostgreSQL"):
        CreateTable(table).compile(dialect=sqlite.dialect())


def test_sequence_lifecycle_is_per_table_and_column():
    statements = []
    engine = create_mock_engine(
        "postgresql://",
        lambda stmt, *a, **k: statements.append(
            str(stmt.compile(dialect=postgresql.dialect()))
        ),
    )
    for table in (First.__table__, Second.__table__):
        table.create(engine)
        table.drop(engine)
    sql = "\n".join(statements)
    for name in ("permuted_first", "permuted_second"):
        for column in ("key", "code"):
            assert f"CREATE SEQUENCE {name}_{column}_seq" in sql
            assert f"DROP SEQUENCE IF EXISTS {name}_{column}_seq" in sql
    assert "NO CYCLE" in sql
    assert sql.index("CREATE SEQUENCE") < sql.index("CREATE TABLE")
    assert sql.index("DROP TABLE") < sql.index("DROP SEQUENCE")


def test_existing_entity_uses_only_one_id():
    assert set(Record.__table__.columns.keys()) == {
        "id",
        "created_at",
        "updated_at",
    }
    value = Record()
    assert value.to_row() == {}
    assert value.to_changes() == {}
    assert Record(id=123).to_row() == {"id": 123}


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
        PermutedBigIntField(**options)


def test_native_column_options_and_quoted_schema():
    class Coded(BaseEntity, table=True):
        __tablename__ = "Unit Quoted Table"
        __table_args__ = {"schema": "Mixed Schema", "extend_existing": True}
        id: int = IDField()
        code: int = PermutedBigIntField(
            digits=6,
            db_column="Public Code",
            unique=True,
            nullable=False,
            comment="External code",
        )

    column = Coded.__table__.c["Public Code"]
    assert column.unique and column.comment == "External code"
    ddl = str(
        CreateTable(Coded.__table__).compile(dialect=postgresql.dialect())
    )
    assert '"Mixed Schema"."Unit Quoted Table_Public Code_seq"' in ddl
    cloned = Coded.__table__.to_metadata(MetaData())
    assert (
        str(CreateTable(cloned).compile(dialect=postgresql.dialect())) == ddl
    )


def test_loaded_identity_is_not_lost_when_only_changed_fields_are_dumped():
    row = Record.model_construct(id=1_234_567_890, _fields_set=set())
    assert row.to_row() == {"id": 1_234_567_890}
    assert row.to_changes() == {}


@pytest.mark.parametrize(
    "options",
    [
        {"default": 123},
        {"default_factory": lambda: 123},
        {"server_default": "123"},
        {"sa_column_kwargs": {"server_default": "123"}},
    ],
)
def test_competing_defaults_are_rejected(options):
    with pytest.raises(ValueError, match="owns its database default"):
        PermutedBigIntField(**options)


def test_other_backend_create_rejects_before_creating_schema_objects():
    from sqlalchemy import create_engine, inspect

    engine = create_engine("sqlite://")
    try:
        with pytest.raises(CompileError, match="PostgreSQL"):
            First.__table__.create(engine)
        assert inspect(engine).get_table_names() == []
    finally:
        engine.dispose()
