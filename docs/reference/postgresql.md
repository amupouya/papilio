# postgresql

Generated from this checkout. Code blocks show signatures; `...` replaces implementation bodies. These are reference declarations, not standalone executable modules.

Single-underscore methods are protected extension tools. For inherited methods, follow the base class reference. Localized string values use Unicode escapes.

Shared implementations: [repository base](repository-base.md). Abstract obligations: [backend contracts](contracts.md).

## Stored public IDs (PostgreSQL only)

Requires the separately installed permuteseq server extension. See
[PostgreSQL public IDs](../guide/public-ids.md) for setup and migration.

```python
# papilio.infra.db.schema.postgresql
def PGPublicIDField(*, digits: int | None = None,
                    start: int | None = None, end: int | None = None) -> Any: ...

class PGPublicIdentifiedEntity(IdentifiedEntity):
    public_id: int = PGPublicIDField()
    def to_row(self, *, exclude_unset: bool = True) -> dict[str, Any]: ...
    def to_changes(self) -> dict[str, Any]: ...

class PGPublicPersistenceEntity(PGPublicIdentifiedEntity, PersistenceEntity): ...

# papilio.infra.db.repositories.backends.postgresql
class PGPublicIdentifiedRepository[T: PGPublicIdentifiedEntity](
    PGIdentifiedRepository[T], PGPublicIdentifiedRepositoryContract[T]
):
    async def get_by_public_id(self, public_id: int) -> T | None: ...
    async def get_by_public_ids(self, public_ids: Sequence[int]) -> Sequence[T]: ...
    async def update_by_public_id(self, public_id: int,
                                  changes: Mapping[str, Any]) -> T | None: ...
    async def update_by_public_ids(self, public_ids: Sequence[int],
                                   changes: Mapping[str, Any]) -> Sequence[T]: ...
    async def remove_by_public_id(self, public_id: int) -> T | None: ...
    async def remove_by_public_ids(self, public_ids: Sequence[int]) -> Sequence[T]: ...

class PGPublicPersistenceRepository[T: PGPublicPersistenceEntity](
    PGPublicIdentifiedRepository[T], PGPersistenceRepository[T],
    PGPublicPersistenceRepositoryContract[T]
): ...
```

Matching abstract contracts are in
papilio.infra.db.repositories.contracts.postgresql. Internal-ID operations keep
their existing semantics. These classes do not install extensions or run DDL.

## `papilio.infra.db.repositories.backends.postgresql`

### `PGReader`

```python
class PGReader(Reader[PGUnitOfWork], PGReaderContract):

    def __init__(self, uow: PGUnitOfWork) -> None:
        ...
```

### `PGRepository`

```python
class PGRepository[T: BaseEntity](Repository[T, PGUnitOfWork], PGRepositoryContract[T]):

    def __init__(self, uow: PGUnitOfWork):
        ...

    def _upsert_stmt(self, row: Mapping[str, Any], *, conflict_columns: Sequence[ColumnClause[Any]], update_columns: Sequence[ColumnClause[Any]], changes: Mapping[ColumnClause[Any], Any] | None=None) -> Insert:
        ...

    def _bulk_upsert_stmt(self, rows: Sequence[Mapping[str, Any]], *, insert_columns: Mapping[str, ColumnClause[Any]], conflict_columns: Sequence[ColumnClause[Any]], update_columns: Sequence[ColumnClause[Any]], changes: Mapping[ColumnClause[Any], Any] | None=None) -> Insert:
        ...

    async def upsert(self, data: T, *, conflict_columns: Sequence[ColumnClause[Any]], update_columns: Sequence[ColumnClause[Any]], changes: Mapping[ColumnClause[Any], Any] | None=None) -> T:
        ...

    async def bulk_upsert(self, data: Sequence[T], *, insert_columns: Mapping[str, ColumnClause[Any]], conflict_columns: Sequence[ColumnClause[Any]], update_columns: Sequence[ColumnClause[Any]], changes: Mapping[ColumnClause[Any], Any] | None=None) -> Sequence[T]:
        ...

    def _values_grid(self, rows: Sequence[Mapping[str, Any]], *, columns: Sequence[ColumnClause[Any]], name: str='incoming') -> Values:
        ...

    def _bulk_update_stmt(self, rows: Sequence[Mapping[str, Any]], *, key_columns: Sequence[ColumnClause[Any]], update_columns: Sequence[ColumnClause[Any]]) -> Update:
        ...

    async def create(self, data: T) -> T:
        ...

    async def bulk_create(self, data: Sequence[T]) -> Sequence[T]:
        ...

    async def bulk_update(self, rows: Sequence[Mapping[str, Any]], *, key_columns: Sequence[ColumnClause[Any]], update_columns: Sequence[ColumnClause[Any]]) -> Sequence[T]:
        ...

    async def get_one(self, *where: ColumnElement[bool]) -> T | None:
        ...

    async def get_all(self, *where: ColumnElement[bool]) -> Sequence[T]:
        ...

    def get_all_stream(self, batch_size: int=100, *, where: Sequence[ColumnElement[bool]]=()) -> AsyncIterator[T]:
        ...

    async def exists(self, *where: ColumnElement[bool]) -> bool:
        ...

    async def count(self, *where: ColumnElement[bool]) -> int:
        ...

    async def get_page(self, *, order_by: Sequence[ColumnElement[Any]], limit: int, offset: int=0, where: Sequence[ColumnElement[bool]]=()) -> PagedType[T]:
        ...

    async def update(self, where: ColumnElement[bool], changes: Mapping[str, Any]) -> Sequence[T]:
        ...

    async def remove(self, where: ColumnElement[bool]) -> Sequence[T]:
        ...
```

### `PGIdentifiedRepository`

```python
class PGIdentifiedRepository[T: IdentifiedEntity](PGRepository[T], IdentifiedRepository[T, PGUnitOfWork], PGIdentifiedRepositoryContract[T]):

    async def get_by_id(self, id: int) -> T | None:
        ...

    async def get_by_ids(self, ids: Sequence[int]) -> Sequence[T]:
        ...

    async def get_paged(self, limit: int, offset: int=0) -> PagedType[T]:
        ...

    async def update_by_id(self, id: int, changes: Mapping[str, Any]) -> T | None:
        ...

    async def update_by_ids(self, ids: Sequence[int], changes: Mapping[str, Any]) -> Sequence[T]:
        ...

    async def update_row_by_id(self, id: int, data: T) -> T | None:
        ...

    async def remove_by_id(self, id: int) -> T | None:
        ...

    async def remove_by_ids(self, ids: Sequence[int]) -> Sequence[T]:
        ...
```

### `PGTimestampRepository`

```python
class PGTimestampRepository[T: TimestampEntity](PGRepository[T], TimestampRepository[T, PGUnitOfWork], PGTimestampRepositoryContract[T]):
    ...
```

### `PGPersistenceRepository`

```python
class PGPersistenceRepository[T: PersistenceEntity](PGIdentifiedRepository[T], PGTimestampRepository[T], PersistenceRepository[T, PGUnitOfWork], PGPersistenceRepositoryContract[T]):
    ...
```
