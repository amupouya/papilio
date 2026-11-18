from enum import Enum, IntEnum, StrEnum

import pytest
from sqlalchemy import create_engine, insert, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import CreateEnumType
from sqlalchemy.schema import CreateTable
from sqlalchemy.types import Enum as SAEnum
from sqlmodel import Session, select

from papilio.infra.db.schema.entity import IdentifiedEntity
from papilio.infra.db.schema.fields import EnumField, NativeEnumField
from papilio.infra.db.table import BaseTable


class Kind(StrEnum):
    EARNING = "earning"
    DEDUCTION = "deduction"


class TextEnumRow(IdentifiedEntity, BaseTable, table=True):
    table_name = "enum_text_probe"

    kind: Kind = EnumField(
        Kind,
        length=35,
        default=Kind.EARNING,
    )
    optional_kind: Kind | None = EnumField(
        Kind,
        nullable=True,
        default=None,
    )


class NativeEnumRow(IdentifiedEntity, BaseTable, table=True):
    table_name = "enum_native_probe"
    kind: Kind = NativeEnumField(Kind)


class NamedNativeEnumRow(IdentifiedEntity, BaseTable, table=True):
    table_name = "enum_named_native_probe"
    kind: Kind = NativeEnumField(
        Kind,
        name="item_kind",
        schema="inventory",
        default=Kind.EARNING,
        server_default=Kind.EARNING.value,
        db_column="item_type",
        index=True,
    )


@pytest.fixture
def engine():
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE enum_text_probe (id INTEGER PRIMARY KEY, "
                "kind VARCHAR(35) NOT NULL, optional_kind VARCHAR(9))"
            )
        )
    try:
        yield engine
    finally:
        engine.dispose()


def test_text_mapping_keeps_postgresql_varchar_without_enum_constraint():
    ddl = str(
        CreateTable(TextEnumRow.__table__).compile(
            dialect=postgresql.dialect()
        )
    )
    assert "kind VARCHAR(35) NOT NULL" in ddl
    assert "optional_kind VARCHAR(9)" in ddl
    assert "CHECK" not in ddl


def test_existing_text_rows_load_members_without_serialization_warning(engine):
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO enum_text_probe (id, kind, optional_kind) "
                "VALUES (1, 'earning', 'deduction')"
            )
        )
    with Session(engine) as session:
        row = session.exec(select(TextEnumRow)).one()
        assert row.kind is Kind.EARNING
        assert row.optional_kind is Kind.DEDUCTION
        assert row.model_dump(mode="json", warnings="error") == {
            "id": 1,
            "kind": "earning",
            "optional_kind": "deduction",
        }


@pytest.mark.parametrize("value", [Kind.DEDUCTION, "deduction"])
def test_writes_store_values_and_reads_restore_members(engine, value):
    with engine.begin() as connection:
        connection.execute(insert(TextEnumRow).values(id=1, kind=value))
        stored = connection.execute(
            text("SELECT kind, optional_kind FROM enum_text_probe")
        ).one()
        assert tuple(stored) == ("deduction", None)
    with Session(engine) as session:
        row = session.exec(select(TextEnumRow)).one()
        assert row.kind is Kind.DEDUCTION
        assert row.optional_kind is None
        row.model_dump(warnings="error")


def test_enum_default_and_nullable_field_round_trip(engine):
    with engine.begin() as connection:
        connection.execute(insert(TextEnumRow).values(id=1))
    with Session(engine) as session:
        row = session.exec(select(TextEnumRow)).one()
        assert row.kind is Kind.EARNING
        assert row.optional_kind is None


def test_unknown_stored_value_is_rejected_without_rewriting_it(engine):
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO enum_text_probe (id, kind) VALUES (1, 'bad')")
        )
    with Session(engine) as session:
        with pytest.raises(LookupError, match="bad"):
            session.exec(select(TextEnumRow)).one()
    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT kind FROM enum_text_probe")
            ).scalar_one()
            == "bad"
        )


def test_native_mapping_stores_values_and_restores_members():
    column_type = NativeEnumRow.__table__.c.kind.type
    assert isinstance(column_type, SAEnum)
    assert column_type.native_enum is True
    assert column_type.enums == ["earning", "deduction"]
    bind = column_type.bind_processor(postgresql.dialect())
    read = column_type.result_processor(postgresql.dialect(), None)
    assert bind is not None
    assert read is not None
    assert bind(Kind.EARNING) == "earning"
    assert read("earning") is Kind.EARNING
    ddl = str(
        CreateEnumType(column_type).compile(dialect=postgresql.dialect())
    )
    assert ddl == "CREATE TYPE kind AS ENUM ('earning', 'deduction')"


def test_native_type_name_schema_and_column_options_are_independent():
    column = NamedNativeEnumRow.__table__.c.item_type
    ddl = str(
        CreateEnumType(column.type).compile(dialect=postgresql.dialect())
    )
    assert ddl == (
        "CREATE TYPE inventory.item_kind AS ENUM ('earning', 'deduction')"
    )
    table_ddl = str(
        CreateTable(NamedNativeEnumRow.__table__).compile(
            dialect=postgresql.dialect()
        )
    )
    assert (
        "item_type inventory.item_kind DEFAULT 'earning' NOT NULL" in table_ddl
    )
    assert column.index is True


def test_native_mapping_falls_back_to_text_on_sqlite():
    engine = create_engine("sqlite://")
    try:
        NativeEnumRow.__table__.create(engine)
        with Session(engine) as session:
            row = NativeEnumRow(kind=Kind.DEDUCTION)
            session.add(row)
            session.commit()
            session.expire_all()
            assert (
                session.exec(select(NativeEnumRow)).one().kind
                is Kind.DEDUCTION
            )
        with engine.connect() as connection:
            stored = connection.execute(
                text("SELECT kind FROM enum_native_probe")
            ).scalar_one()
            assert stored == "deduction"
    finally:
        engine.dispose()


@pytest.mark.parametrize("field", [EnumField, NativeEnumField])
def test_string_enum_aliases_round_trip_with_one_stored_label(field):
    class Aliased(str, Enum):
        FIRST = "first"
        ALSO_FIRST = "first"
        SECOND = "second"

    enum_type = field(Aliased).sa_type
    bind = enum_type.bind_processor(postgresql.dialect())
    read = enum_type.result_processor(postgresql.dialect(), None)
    assert enum_type.enums == ["first", "second"]
    assert bind(Aliased.ALSO_FIRST) == "first"
    assert read("first") is Aliased.FIRST


@pytest.mark.parametrize("field", [EnumField, NativeEnumField])
def test_non_string_values_are_rejected_when_declaring_a_field(field):
    class Number(IntEnum):
        ONE = 1

    with pytest.raises(TypeError, match="string"):
        field(Number)


@pytest.mark.parametrize("length", [0, 3, 8])
def test_length_shorter_than_stored_values_is_rejected(length):
    with pytest.raises(ValueError, match="length"):
        EnumField(Kind, length=length)
