"""Enum field persistence in an isolated real PostgreSQL schema."""

import os
from enum import StrEnum
from uuid import uuid4

import pytest
from sqlalchemy import insert, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema
from sqlmodel import select

from papilio.infra.db.schema.entity import IdentifiedEntity
from papilio.infra.db.schema.fields import EnumField, NativeEnumField
from papilio.infra.db.table import BaseTable

SCHEMA = "enum_fields_test"


class Kind(StrEnum):
    EARNING = "earning"
    DEDUCTION = "deduction"


class EnumValuesRow(IdentifiedEntity, BaseTable, table=True):
    table_name = "enum_values"
    __table_args__ = {"schema": SCHEMA}

    ordinary: Kind = EnumField(Kind, length=35, default=Kind.EARNING)
    native: Kind = NativeEnumField(
        Kind, name="item_kind", schema=SCHEMA, default=Kind.EARNING
    )
    optional: Kind | None = NativeEnumField(
        Kind, name="item_kind", schema=SCHEMA, nullable=True, default=None
    )
    server: Kind = NativeEnumField(
        Kind, name="item_kind", schema=SCHEMA, server_default="earning"
    )


@pytest.fixture
async def enum_store():
    dsn = os.environ.get("PAPILIO_TEST_POSTGRESQL")
    if not dsn:
        pytest.skip("Set PAPILIO_TEST_POSTGRESQL for live enum field tests")
    schema = f"enum_fields_{uuid4().hex}"
    engine = create_async_engine(
        dsn,
        execution_options={"schema_translate_map": {SCHEMA: schema}},
    )
    created = False
    try:
        async with engine.begin() as connection:
            await connection.execute(CreateSchema(schema))
            await connection.run_sync(EnumValuesRow.__table__.create)
        created = True
        yield engine, schema
    finally:
        try:
            if created:
                async with engine.begin() as connection:
                    await connection.execute(DropSchema(schema, cascade=True))
        finally:
            await engine.dispose()


async def test_native_and_text_values_defaults_and_nulls_round_trip(
    enum_store,
):
    engine, schema = enum_store
    async with AsyncSession(engine, expire_on_commit=False) as session:
        session.add(EnumValuesRow(id=1))
        session.add(
            EnumValuesRow(
                id=2,
                ordinary=Kind.DEDUCTION,
                native=Kind.DEDUCTION,
                optional=Kind.DEDUCTION,
            )
        )
        await session.commit()
    async with engine.begin() as connection:
        await connection.execute(
            insert(EnumValuesRow).values(
                id=3, ordinary="deduction", native="deduction"
            )
        )
        raw = await connection.exec_driver_sql(
            f"SELECT ordinary, native::text, optional::text, server::text "
            f'FROM "{schema}".enum_values ORDER BY id'
        )
        assert raw.all() == [
            ("earning", "earning", None, "earning"),
            ("deduction", "deduction", "deduction", "earning"),
            ("deduction", "deduction", None, "earning"),
        ]
        labels = await connection.execute(
            text(
                "SELECT e.enumlabel FROM pg_enum e "
                "JOIN pg_type t ON e.enumtypid = t.oid "
                "JOIN pg_namespace n ON t.typnamespace = n.oid "
                "WHERE n.nspname = :schema AND t.typname = 'item_kind' "
                "ORDER BY e.enumsortorder"
            ),
            {"schema": schema},
        )
        assert labels.scalars().all() == ["earning", "deduction"]
    async with AsyncSession(engine) as session:
        result = await session.execute(
            select(EnumValuesRow).order_by(EnumValuesRow.id)
        )
        rows = result.scalars().all()
        assert rows[0].ordinary is rows[0].native is Kind.EARNING
        assert rows[0].optional is None
        assert rows[0].server is Kind.EARNING
        for row in rows[1:]:
            assert row.ordinary is row.native is Kind.DEDUCTION
            row.model_dump(mode="json", warnings="error")


async def test_database_rejects_unknown_native_label(enum_store):
    engine, _ = enum_store
    with pytest.raises(DBAPIError, match="bad"):
        async with engine.begin() as connection:
            await connection.execute(
                insert(EnumValuesRow).values(id=1, native="bad")
            )
    async with AsyncSession(engine) as session:
        result = await session.execute(select(EnumValuesRow))
        assert result.scalars().all() == []
