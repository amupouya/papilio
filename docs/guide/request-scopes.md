# Request-scoped function execution

`request_scoped` gives an async execution method a short native Dishka REQUEST
scope. Its resources live until that method finishes, including on failure or
cancellation. Business services and repositories keep constructor injection.

## Decorate an execution method

Given an APP container with the normal database and service providers:

```python
from dishka import AsyncContainer, Provider, Scope, provide

from papilio.infra.scopes import Inject, request_scoped


class PriceExecution:
    def __init__(self, container: AsyncContainer):
        self.container = container

    @request_scoped
    async def calculate(
        self,
        variant_id: int,
        *,
        currency: str = "IRR",
        pricing: PricingService = Inject(),
    ) -> Price:
        return await pricing.calculate(variant_id, currency=currency)


class ExecutionProvider(Provider):
    execution = provide(PriceExecution, scope=Scope.APP)


# Register ExecutionProvider with the application's ordinary providers.
execution = await container.get(PriceExecution)
price = await execution.calculate(variant_id, currency="IRR")
```

`PricingService` and `Price` are application types. The APP `AsyncContainer`
is supplied by Dishka to the constructor. No additional scope provider or global
application container is needed. APP dependencies can remain in the constructor;
REQUEST dependencies belong to the decorated method so they are created inside
its short scope. Passing an HTTP request container as `self.container` is rejected.
An already-created constructor dependency cannot be moved to a new scope.

`Inject()` is Papilio's declaration marker. Its annotated type selects the native
Dishka dependency; providers still own construction, caching and finalization.
Injected parameters must be keyword-only, after `*`, and have a type annotation.
Multiple dependencies, `Annotated` types and native Dishka components work.
Annotation and signature inspection happen once, when the decorator is applied.

Ordinary positional/named arguments, defaults and return types retain their
static types, including async callable Protocols. There is no extra payload type
or input DTO requirement. A marker is a placeholder default, replaced before the
method runs. Static typing keeps dependency parameters optional; it does not
remove them from the callable type. They are reserved for injection: explicitly
supplying one raises `TypeError`. The runtime signature hides them.

Dependency resolution is lazy: a database session opens only if a dependency
needs it. Put the decorator around each database phase of a workflow so external
network calls can run between short scopes. Shared APP pools/HTTP/Redis clients
remain open until their normal application shutdown.

## Bind a function explicitly

The existing container-getter API remains available for functions and adapters
that need an explicit source:

```python
@request_scoped(container_getter=lambda args, kwargs: container)
async def calculate_price(
    variant_id: int, *, pricing: PricingService = Inject()
) -> Price:
    return await pricing.calculate(variant_id)


price = await calculate_price(variant_id)
```

The getter receives the original positional tuple and keyword dictionary, using
Dishka's `ContainerGetter[AsyncContainer]` contract. Native
`pricing: FromDishka[PricingService]` annotations remain supported; add an
`Inject()` default if a direct typed caller needs to omit that parameter.

## Ownership and nesting

- Bare methods use the APP container and own a new REQUEST for an independent
  call, even inside an ordinary HTTP request. Only the owner closes it.
- Same-task nested decorated calls reuse the active request when their source
  is that request or one of its ancestors. Different roots keep separate scopes.
- Concurrent and child tasks using the APP container get independent requests
  and UoWs. A child cannot join its parent's application transaction.
- An explicit getter returning a REQUEST borrows it; the caller owns its lifetime.
  Never share it across tasks or use it after scope exit. The adapter rejects
  inherited requests it can identify; external request ownership is the caller's
  responsibility.

Cleanup is shielded from AnyIO cancellation. SQL UoW cleanup also retains its
native asyncio cancellation handling. Exceptions propagate; this adapter does
not retry or translate failures.

## Transactions stay explicit

The decorator owns resource lifetime. It does not commit or open an application
transaction. Uncommitted SQL writes are discarded when the session closes.
Normally the injected service owns its existing `@transactional` operation.
To coordinate one transaction across several services, compose the decorators:

```python
from papilio.infra.db.tools.decorators import transactional


class StockExecution:
    def __init__(self, container: AsyncContainer):
        self.container = container

    @request_scoped
    @transactional
    async def update(
        self, product_id: int, *, service: StockService = Inject()
    ) -> StockResult:
        return await service.update(product_id)
```

Put `@request_scoped` outside `@transactional`. Here the registered `StockService`
depends on a UoW, so injection opens it before the transaction wrapper runs.
Nested transactions retain rollback-only behavior and only their outer owner
commits. See [transactions](transactions.md).

## Result lifetime

Owned scopes close before results reach the caller. Return materialized domain
results. Lazy ORM relationships, cursors, iterators and live services need an
owner that remains open. Streaming requires its own lifetime. Synchronous
functions and async generators are rejected when the decorator is applied.

Native HTTP, tool and task integrations already owning the appropriate scope
usually need no extra decorator. Use a separate operation boundary when its
resources should be released before that outer request finishes.
