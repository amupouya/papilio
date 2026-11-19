# Stored public IDs on PostgreSQL

This capability is **PostgreSQL-only** and requires the
[permuteseq extension](https://github.com/dverite/permuteseq). It is optional;
ordinary entities and other database backends keep their existing behavior.

The database computes a unique, random-looking public number from the existing
positive primary key and stores it in a generated BIGINT column. There is no
Python encoding on reads, collision-retry insertion loop, extra sequence, runtime
secret or encryption JSON. This is identifier obfuscation, not cryptographic
protection or authorization. The generator has internal Feistel/cycle-walking
iterations; it is not a single arithmetic operation.

## Prepare PostgreSQL

Install permuteseq on the database server before creating dependent tables.
The exercised upstream source is 1.2.2, commit
`15f86f650002bca5b79a9f85a678388d6999efde`. PostgreSQL's ordinary images do not
include it. Its upstream PGXS build needs the server's matching development
headers and a C compiler. Database administrators own that server installation;
Papilio does not install extensions or execute DDL during import/startup.

An explicit application migration enables the installed extension first:

```sql
CREATE EXTENSION IF NOT EXISTS permuteseq;
```

For Papilio's disposable PostgreSQL test container, the pinned/checksummed CI
installer is `python scripts/ci/install_permuteseq.py CONTAINER_ID`. This installs
build dependencies inside that explicitly selected disposable Debian container;
it is not an application startup command.

## Define the entity and repository

```python
from papilio.infra.db.repositories.backends.postgresql import (
    PGPublicIdentifiedRepository,
)
from papilio.infra.db.schema.postgresql import (
    PGPublicIdentifiedEntity,
    PGPublicIDField,
)
from papilio.infra.db.table import BaseTable


class ProductModel(PGPublicIdentifiedEntity):
    public_id: int = PGPublicIDField(digits=10)
    title: str


class ProductTable(ProductModel, BaseTable, table=True):
    pass


class ProductRepository(PGPublicIdentifiedRepository[ProductModel]):
    table = ProductTable
```

The inherited public_id already defaults to 10 digits; omit its redeclaration
when that fits. For timestamps, select PGPublicPersistenceEntity and
PGPublicPersistenceRepository from the same PostgreSQL modules.

For an exact interval, use PGPublicIDField(start=1_000_000_000,
end=1_999_999_999), without digits. Decimal width supports 1 through 18 digits;
explicit positive ranges must fit signed BIGINT and contain at least four
values. Ranges and column generation expressions are schema declarations, not
deployment configuration. Separate ranges give different resources separate
public number spaces; uniqueness is otherwise per table, not global.

The fixed permutation parameter is zero: it is not a secret and needs no user
configuration. For a capacity N, the database permutes internal IDs 1 through N
and shifts them into the chosen output interval. IDs outside that capacity fail
the insert. Gaps from rollbacks/deletions still consume internal sequence space;
capacity is not the current row count. Keep primary keys immutable, never reset
their sequence to reused values, and migrate explicitly before changing ranges
or generator implementations. Do not assume mappings survive algorithm changes.

The generated column is STORED and UNIQUE. PostgreSQL rejects direct writes to
public_id. The entity's to_row() and to_changes() omit it, including when saving
an already loaded row. Native INSERT RETURNING and bulk inserts return it without
a separate lookup. Updates recompute stored generated expressions inside the DB;
reads do not. Internal get_by_id/update_by_id methods retain their meaning.

Public operations are get_by_public_id(s), update_by_public_id(s), and
remove_by_public_id(s). Single mutations return a row or None; set operations
return matching rows without an ordering guarantee. Transactions remain owned
by the application's existing UnitOfWork/transaction boundary.

## Outputs, foreign references and errors

```python
from pydantic import Field
from papilio.schemas.outputs import PublicIDOutput


class ProductOutput(PublicIDOutput):
    title: str
    category_id: int | None = Field(validation_alias="category_public_id")
```

PublicIDOutput reads public_id and emits id through native Pydantic aliasing.
It deliberately does not fall back to an object's internal id. No database
package is needed to import this output schema.

A query for the example must select the product's public_id and the category's
public_id labelled category_public_id, using an explicit join (outer join for a
nullable reference). Keep stored foreign keys internal. Materialize public
references together in a join or bounded set query; never perform a database
lookup in a Pydantic validator or one query per output item. Nested/list/filter
outputs must likewise receive the owning resource's stored public values.

Validate public inputs as ordinary positive integer fields, then query directly
by public_id. Do not decode them. For a missing public lookup, construct the
normal NotFoundException with the requested public value in identifier_value
and in the message; the HTTP handler performs no hidden ID conversion. Custom
validation errors must also use public values in input, ctx and messages.
Authorization and parent/ownership predicates remain application responsibilities.

The feature adds a stored BIGINT and unique index and moves generation to writes.
It does not promise lower end-to-end latency than an existing integer lookup or
Python mapping. Measure the actual write/read workload before claiming a speedup.
