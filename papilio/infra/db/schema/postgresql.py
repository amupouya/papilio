"""PostgreSQL fields backed by native sequence defaults."""

from typing import Any, Unpack

from sqlalchemy import (
    DDL,
    BigInteger,
    Column,
    ColumnDefault,
    DefaultClause,
    FetchedValue,
    Sequence,
    String,
    Table,
    event,
    literal,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import CompileError
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.schema import CreateSequence, DropSequence
from sqlalchemy.sql.compiler import SQLCompiler
from sqlalchemy.sql.functions import FunctionElement

from papilio.infra.db.schema.fields import FieldOptions, _field


class _PermutedDefault(FunctionElement[int]):
    type = BigInteger()
    inherit_cache = True


@compiles(_PermutedDefault)
def _unsupported_permutation(
    expression: _PermutedDefault, compiler: SQLCompiler, **kwargs: Any
) -> str:
    raise CompileError(
        "PermutedBigIntField requires PostgreSQL and permuteseq"
    )


@compiles(_PermutedDefault, "postgresql")
def _postgresql_permutation(
    expression: _PermutedDefault, compiler: SQLCompiler, **kwargs: Any
) -> str:
    name = compiler.process(expression.clauses, **kwargs)
    return f"permute_nextval({name}::regclass, 0)"


def PermutedBigIntField(
    *,
    digits: int | None = None,
    start: int | None = None,
    end: int | None = None,
    **options: Unpack[FieldOptions],
) -> Any:
    """Declare an independently generated, nonsequential BIGINT column.

    Args:
        digits: Decimal width, default 10. Exclusive with start/end.
        start: Inclusive positive lower bound of an explicit range.
        end: Inclusive upper bound, at most signed BIGINT's maximum.
        **options: Native field options. The caller chooses primary_key and
            unique. SQL defaults execute in PostgreSQL without a Python
            generator.

    Returns:
        A field with a bounded NO CYCLE sequence and a permuteseq default.

    Raises:
        ValueError: Bounds or competing default options are invalid.
        CompileError: The table is compiled for another database backend.
    """
    if start is None and end is None:
        width = 10 if digits is None else digits
        if type(width) is not int or not 1 <= width <= 18:
            raise ValueError("digits must be an integer between 1 and 18")
        start, end = 10 ** (width - 1), 10**width - 1
    elif digits is not None:
        raise ValueError("Choose digits or start/end, not both")
    if (
        type(start) is not int
        or type(end) is not int
        or start < 1
        or end > 2**63 - 1
        or end - start + 1 < 4
    ):
        raise ValueError("start/end must define 4 or more positive BIGINTs")
    if (
        options.get("default") is not None
        or options.get("default_factory") is not None
        or options.get("server_default") is not None
        or (options.get("sa_column_kwargs") or {}).get("server_default")
        is not None
    ):
        raise ValueError("PermutedBigIntField owns its database default")
    options.setdefault("default", None)

    def attach(column: Column[Any], table: Table) -> None:
        sequence = Sequence(
            f"{table.name}_{column.name}_seq",
            schema=table.schema,
            start=start,
            minvalue=start,
            maxvalue=end,
            cycle=False,
        )
        preparer = postgresql.dialect().identifier_preparer
        name = preparer.format_sequence(sequence)
        expression = _PermutedDefault(literal(name, String()))
        # The SQL expression default also preserves all-default bulk rows.
        column.default = ColumnDefault(expression)
        column.server_default = DefaultClause(expression)
        # Native DDL events own create/drop; sequence ownership also covers
        # external DROP TABLE and TRUNCATE RESTART IDENTITY.
        event.listen(
            table,
            "before_create",
            CreateSequence(sequence).execute_if(dialect="postgresql"),
        )
        event.listen(
            table,
            "after_create",
            DDL(
                "ALTER SEQUENCE %(sequence)s OWNED BY %(fullname)s.%(column)s",
                context={
                    "sequence": name,
                    "column": preparer.quote(column.name),
                },
            ).execute_if(dialect="postgresql"),
        )
        event.listen(
            table,
            "after_drop",
            DropSequence(sequence, if_exists=True).execute_if(
                dialect="postgresql"
            ),
        )

    def prepare(default: FetchedValue, column: Column[Any]) -> None:
        column.autoincrement = False
        event.listen(column, "after_parent_attach", attach, propagate=True)

    generated = FetchedValue()
    event.listen(generated, "after_parent_attach", prepare)
    return _field(BigInteger, generated, **options)
