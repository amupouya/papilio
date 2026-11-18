# pyright: strict, reportUnnecessaryTypeIgnoreComment=error
"""Direct method/function typing, including deliberately rejected calls."""

from typing import Protocol, assert_type

from dishka import AsyncContainer, FromDishka

from papilio.infra.scopes import Inject, request_scoped


class Service:
    async def run(self, value: int) -> str:
        return str(value)


class Execution:
    def __init__(self, container: AsyncContainer):
        self.container = container

    @request_scoped
    async def prepare(
        self,
        request_id: int,
        offset: int = 0,
        *,
        source: str = "sms",
        service: Service = Inject(),
        other: FromDishka[Service] = Inject(),
    ) -> str:
        assert service is other
        return source + await service.run(request_id + offset)


class Prepare(Protocol):
    async def __call__(
        self, request_id: int, offset: int = 0, *, source: str = "sms"
    ) -> str: ...


def check_function_binding(container: AsyncContainer) -> Prepare:
    @request_scoped(container_getter=lambda args, kwargs: container)
    async def prepare(
        request_id: int,
        offset: int = 0,
        *,
        source: str = "sms",
        service: Service = Inject(),
    ) -> str:
        return source + await service.run(request_id + offset)

    return prepare


async def check_calls(execution: Execution) -> None:
    assert_type(await execution.prepare(1), str)
    assert_type(await execution.prepare(request_id=1, offset=2), str)
    assert_type(await execution.prepare(1, 2, source="api"), str)
    prepare: Prepare = execution.prepare
    assert_type(await prepare(1), str)

    await execution.prepare("wrong")  # pyright: ignore[reportArgumentType]
    await execution.prepare()  # pyright: ignore[reportCallIssue]
    await execution.prepare(1, offset="wrong")  # pyright: ignore[reportArgumentType]
    await execution.prepare(1, source=42)  # pyright: ignore[reportArgumentType]
    await execution.prepare(1, 2, "extra")  # pyright: ignore[reportCallIssue]
    await execution.prepare(1, unknown=True)  # pyright: ignore[reportCallIssue]
