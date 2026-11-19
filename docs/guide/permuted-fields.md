# Permuted BIGINT fields on PostgreSQL

`PermutedBigIntField` is an optional **PostgreSQL-only field helper** requiring
[permuteseq](https://github.com/dverite/permuteseq). It generates a stored,
nonsequential integer directly in the declared column. It does not add a second
identity, select a primary key, or require a special entity/repository/output.

## Declare a field

```python
from papilio.infra.db.schema.entity import BaseEntity
from papilio.infra.db.schema.fields import IDField
from papilio.infra.db.schema.postgresql import PermutedBigIntField

class Ticket(BaseEntity, table=True):
    id: int = IDField()
    code: int = PermutedBigIntField(digits=8, unique=True)
```

The column may have any name. `primary_key` and `unique` default to false; choose
them explicitly as for other field helpers. Native options such as `db_column`,
`nullable`, `index` and `comment` retain their meaning. Competing client/server
defaults are rejected because this field owns generation.

For the existing ready-to-use repositories, override the normal `id` field:

```python
from papilio.infra.db.repositories.backends.postgresql import PGPersistenceRepository
from papilio.infra.db.schema.entity import PersistenceEntity
from papilio.infra.db.table import BaseTable

class ProductModel(PersistenceEntity):
    id: int = PermutedBigIntField(digits=10, primary_key=True)
    title: str

class ProductTable(ProductModel, BaseTable, table=True):
    pass

class ProductRepository(PGPersistenceRepository[ProductModel]):
    table = ProductTable
```

`IdentifiedEntity` / `PGIdentifiedRepository` work the same way without timestamps.
Ordinary `create`, `bulk_create`, `get_by_id(s)`, `update_by_id(s)`, deletion and
upsert operations retain their contracts. INSERT RETURNING obtains the actual ID;
there is no decoding or extra lookup. Foreign keys store that same value. Use an
ordinary `BaseOutput` with `id: int` and ordinary foreign-reference fields.

## Range and generation

Without range options the decimal width is 10. `digits` accepts 1 through 18;
`start` and `end` instead select an inclusive positive BIGINT interval containing
at least four values. Do not combine those forms:

```python
code: int = PermutedBigIntField(start=1_000_000, end=1_999_999_999, unique=True)
```

Each table/column has its own bounded NO CYCLE sequence, named
`<table>_<database-column>_seq` in that table's schema. Identifier names must fit
PostgreSQL's normal limits. A server DEFAULT calls `permute_nextval(sequence, 0)`:
sequence allocation is concurrent-safe, and a fixed permutation produces each
number in the range at most once while that sequence advances without reuse.
Zero is a fixed public parameter, not a secret. The effect is random-looking,
not cryptographic protection or an authorization mechanism.

Server defaults cover direct SQL inserts; SQLAlchemy also renders the same SQL
expression for omitted values, including batches whose rows have no explicit
columns. Both execute in PostgreSQL, without a Python generator or extra lookup.
Generation runs on insertion when the column is omitted. Reads and ordinary
updates do not recompute it. Explicit inserted values retain native DEFAULT
semantics; the selected PK/UNIQUE constraints protect stored uniqueness. Imported
values can conflict with future generated values, so preserve sequence/generator
ownership when relying on collision-free allocation. Never reset, cycle or change
bounds/parameters on a used sequence. Exhaustion raises instead of retrying or
wrapping. Rollbacks, unused allocations and deletions do not restore capacity.
Uniqueness is per generator/table, not global across resources.

A permuted `id` is not a creation-order cursor. Choose explicit timestamp ordering
when needed. The ready timestamp repositories retain `(created_at, id)` ordering.
This removes the old extra identity column/index; it does not claim a measured
throughput improvement over a sequential primary key.

## PostgreSQL setup and migrations

Install permuteseq on the database server, then enable it in an explicit migration:

```sql
CREATE EXTENSION IF NOT EXISTS permuteseq;
```

The tested pinned source is upstream 1.2.2 at commit
`15f86f650002bca5b79a9f85a678388d6999efde`. Stock PostgreSQL images do not include
it. `python scripts/ci/install_permuteseq.py CONTAINER_ID` builds it only in an
explicitly selected disposable Debian test container; it is not an application
startup command.

Native `Table.create/drop` and `MetaData.create_all/drop_all` run the field's DDL
hooks. A sequence is created before its table and made OWNED BY the column after
creation. External DROP TABLE also removes the owned sequence. No connection or
DDL runs when importing the declaration.

Alembic renders the column default as ordinary `sa.text`, without Papilio imports.
Its native autogenerator does not create sequence operations. Review the revision
and add the sequence before `op.create_table`, then ownership afterward. For a
fresh `products.id` with 10 digits:

```python
import sqlalchemy as sa
from alembic import op

def upgrade():
    op.execute(sa.schema.CreateSequence(sa.Sequence(
        "products_id_seq", start=1_000_000_000,
        minvalue=1_000_000_000, maxvalue=9_999_999_999, cycle=False,
    )))
    op.create_table(
        "products",
        sa.Column("id", sa.BigInteger(), primary_key=True,
                  server_default=sa.text("permute_nextval('products_id_seq'::regclass, 0)")),
        sa.Column("title", sa.Text(), nullable=False),
    )
    op.execute("ALTER SEQUENCE products_id_seq OWNED BY products.id")

def downgrade():
    op.drop_table("products")  # PostgreSQL drops the owned sequence too.
```

Use the actual database table/column names and schema. Adding a field to an
existing table likewise needs explicit sequence/default/ownership DDL. Renaming
a table/column preserves its existing sequence reference; compare the resulting
migration rather than assuming that a declaration renames stored objects.

Changing an existing identifier column or its generator requires an explicit
application migration preserving stored values and dependent foreign keys.
Papilio does not alter existing data or install server extensions at startup.
