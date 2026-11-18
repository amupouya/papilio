"""Short method-owned scopes with native injection and real SQLite sessions."""

import asyncio
import inspect
from collections.abc import AsyncIterator
from typing import Annotated

import pytest
from anyio import create_task_group, sleep
from dishka import (
    AsyncContainer,
    FromComponent,
    FromDishka,
    Provider,
    Scope,
    make_async_container,
    provide,
)
from sqlalchemy import insert
from sqlalchemy.exc import IntegrityError

from papilio.infra.db.tools.decorators import transactional
from papilio.infra.db.transaction import TransactionRollbackOnly
from papilio.infra.db.uow import SQLiteUnitOfWork, UnitOfWork
from papilio.infra.scopes import Inject, request_scoped
from tests.unit.test_request_scoped import (
    RecordService,
    records,
    stored_ids,
)
from tests.unit.test_request_scoped import (
    runtime as runtime,
)


class Execution:
    def __init__(self, container: AsyncContainer):
        self.container = container
        self.units: list[SQLiteUnitOfWork] = []

    @request_scoped
    async def prepare(
        self,
        request_id: int,
        offset: int = 0,
        *,
        source: str = "sms",
        service: RecordService = Inject(),
        unit: FromDishka[SQLiteUnitOfWork] = Inject(),
    ) -> tuple[int, str]:
        """Prepare within a short scope."""
        assert service.unit is unit
        self.units.append(unit)
        return await service.create(request_id + offset), source

    @request_scoped
    async def plain(self, value: int, *, label: str = "default") -> str:
        return f"{value}:{label}"

    @request_scoped
    @transactional
    async def outer(
        self, value: int, *, unit: SQLiteUnitOfWork = Inject()
    ) -> None:
        self.units.append(unit)
        await self.prepare(value)
        assert self.units[-1] is unit
        await unit.execute(insert(records).values(id=value + 1))

    @request_scoped
    @transactional
    async def caught_failure(
        self, value: int, *, unit: SQLiteUnitOfWork = Inject()
    ) -> None:
        await self.prepare(value)
        with pytest.raises(IntegrityError):
            await self.prepare(value)

    @request_scoped
    async def raw_write(
        self, value: int, *, unit: SQLiteUnitOfWork = Inject()
    ) -> None:
        self.units.append(unit)
        await unit.execute(insert(records).values(id=value))

    @request_scoped
    async def parallel(
        self, barrier: asyncio.Barrier, *, unit: SQLiteUnitOfWork = Inject()
    ) -> None:
        self.units.append(unit)
        await asyncio.wait_for(barrier.wait(), 3)

    @request_scoped
    async def child(self, *, unit: SQLiteUnitOfWork = Inject()) -> None:
        self.units.append(unit)
        await asyncio.create_task(self.raw_write(7))
        assert UnitOfWork.current() is unit
        assert unit.is_open


async def test_normal_arguments_defaults_metadata_and_session_cleanup(runtime):
    execution = Execution(runtime.container)
    assert await execution.prepare(1) == (1, "sms")
    assert await execution.prepare(request_id=2, offset=1, source="api") == (
        3,
        "api",
    )
    assert await execution.prepare(4, 1) == (5, "sms")
    assert await stored_ids(runtime) == [1, 3, 5]
    assert len({id(unit) for unit in execution.units}) == 3
    assert all(not unit.is_open for unit in execution.units)
    assert UnitOfWork.current() is None
    assert runtime.database.engine.pool.checkedout() == 0
    assert list(inspect.signature(execution.prepare).parameters) == [
        "request_id",
        "offset",
        "source",
    ]
    assert execution.prepare.__doc__ == "Prepare within a short scope."
    assert (
        inspect.signature(execution.prepare).return_annotation
        == tuple[int, str]
    )


async def test_methods_without_injected_arguments(runtime):
    execution = Execution(runtime.container)
    assert await execution.plain(7) == "7:default"
    assert await execution.plain(7, label="api") == "7:api"


async def test_nested_methods_share_the_outer_transaction(runtime):
    execution = Execution(runtime.container)
    await execution.outer(4)
    assert await stored_ids(runtime) == [4, 5]
    assert execution.units[0] is execution.units[1]
    assert not execution.units[0].is_open


async def test_caught_nested_failure_is_rollback_only(runtime):
    execution = Execution(runtime.container)
    with pytest.raises(TransactionRollbackOnly):
        await execution.caught_failure(4)
    assert await stored_ids(runtime) == []
    assert all(not unit.is_open for unit in execution.units)


async def test_scope_exit_does_not_commit(runtime):
    execution = Execution(runtime.container)
    await execution.raw_write(4)
    assert await stored_ids(runtime) == []
    assert not execution.units[0].is_open


async def test_concurrent_methods_have_independent_sessions(runtime):
    execution = Execution(runtime.container)
    barrier = asyncio.Barrier(2)
    await asyncio.gather(
        execution.parallel(barrier), execution.parallel(barrier)
    )
    assert execution.units[0] is not execution.units[1]
    assert all(not unit.is_open for unit in execution.units)


async def test_child_task_has_its_own_session(runtime):
    execution = Execution(runtime.container)
    await execution.child()
    assert execution.units[0] is not execution.units[1]
    assert all(not unit.is_open for unit in execution.units)
    assert UnitOfWork.current() is None


async def test_default_method_boundary_requires_the_app_container(runtime):
    async with runtime.container() as request:
        execution = Execution(request)
        with pytest.raises(ValueError, match="APP container"):
            await execution.prepare(1)
    assert await stored_ids(runtime) == []


async def test_method_boundary_is_shorter_than_an_outer_http_request(runtime):
    async with runtime.container() as request:
        outer = await request.get(SQLiteUnitOfWork)
        execution = Execution(runtime.container)
        await execution.prepare(1)
        assert execution.units[0] is not outer
        assert not execution.units[0].is_open
        assert outer.is_open


async def test_injected_dependencies_cannot_be_overridden(runtime):
    execution = Execution(runtime.container)
    with pytest.raises(TypeError):
        await execution.prepare(1, service=object())
    assert await stored_ids(runtime) == []
    assert runtime.database.engine.pool.checkedout() == 0


async def test_anyio_cancellation_rolls_back_and_releases_the_connection(
    runtime,
):
    ready = asyncio.Event()
    units = []

    class Waiting(Execution):
        @request_scoped
        @transactional
        async def wait(self, *, unit: SQLiteUnitOfWork = Inject()) -> None:
            units.append(unit)
            await unit.execute(insert(records).values(id=1))
            ready.set()
            await sleep(10)

    execution = Waiting(runtime.container)
    async with create_task_group() as group:
        group.start_soon(execution.wait)
        await ready.wait()
        group.cancel_scope.cancel()
    assert all(not unit.is_open for unit in units)
    assert await stored_ids(runtime) == []
    assert runtime.database.engine.pool.checkedout() == 0


async def test_app_resources_remain_open_between_method_calls():
    closed = []

    class Client:
        pass

    class Resources(Provider):
        @provide(scope=Scope.APP)
        async def client(self) -> AsyncIterator[Client]:
            try:
                yield Client()
            finally:
                closed.append(True)

    class Calls:
        def __init__(self, container: AsyncContainer):
            self.container = container

        @request_scoped
        async def call(self, *, client: Client = Inject()) -> int:
            assert closed == []
            return id(client)

    root = make_async_container(Resources())
    try:
        calls = Calls(root)
        assert await calls.call() == await calls.call()
        assert closed == []
    finally:
        await root.close()
    assert closed == [True]


async def test_constructor_container_is_injected_by_dishka():
    class Executions(Provider):
        execution = provide(Execution, scope=Scope.APP)

    root = make_async_container(Executions())
    try:
        execution = await root.get(Execution)
        assert execution.container is root
        assert await execution.plain(7) == "7:default"
    finally:
        await root.close()


async def test_injection_preserves_native_component_and_annotated_types():
    class Alternate(Provider):
        component = "alternate"

        @provide(scope=Scope.REQUEST)
        def text(self) -> str:
            return "alternate"

    class Default(Provider):
        @provide(scope=Scope.REQUEST)
        def value(self) -> int:
            return 7

    root = make_async_container(Alternate(), Default())
    try:

        @request_scoped(container_getter=lambda args, kwargs: root)
        async def call(
            *,
            text: Annotated[str, FromComponent("alternate")] = Inject(),
            value: Annotated[int, "ordinary metadata"] = Inject(),
        ) -> tuple[str, int]:
            return text, value

        assert await call() == ("alternate", 7)
    finally:
        await root.close()


def test_injected_parameters_must_be_keyword_only():
    with pytest.raises(TypeError, match="keyword-only"):

        @request_scoped
        async def invalid(self, service: RecordService = Inject()) -> None:
            pass


def test_injected_parameter_requires_an_annotation():
    with pytest.raises(TypeError, match="annotation"):

        @request_scoped
        async def invalid(self, *, service=Inject()) -> None:
            pass
