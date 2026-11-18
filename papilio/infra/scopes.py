"""Method and function execution in native Dishka request scopes."""

import asyncio
import inspect
import sys
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from functools import partial, wraps
from types import CoroutineType
from typing import Any, cast, overload

from anyio import CancelScope
from dishka import AsyncContainer, Scope
from dishka.entities.component import DEFAULT_COMPONENT
from dishka.entities.key import DependencyKey, hint_to_dependency_key
from dishka.integrations.base import (
    ContainerGetter,
    default_parse_dependency,
    wrap_injection,
)

_INJECT = object()
_SCOPE_ARG = "_papilio_scope"


def Inject() -> Any:
    """Mark a typed keyword-only argument for injection by request_scoped."""
    return _INJECT


def _dependency(
    parameter: inspect.Parameter, hint: Any
) -> DependencyKey | None:
    key = default_parse_dependency(parameter, hint)
    if key is None and parameter.default is _INJECT:
        if parameter.annotation is inspect.Parameter.empty:
            raise TypeError("Inject() requires a type annotation")
        key = hint_to_dependency_key(hint).with_component(DEFAULT_COMPONENT)
    if (
        key is not None
        and parameter.kind is not inspect.Parameter.KEYWORD_ONLY
    ):
        raise TypeError("Injected parameters must be keyword-only (after *)")
    return key


@dataclass
class _Execution:
    source: AsyncContainer
    request: AsyncContainer
    owner: asyncio.Task[Any] | None
    active: bool = True


_executions: ContextVar[tuple[_Execution, ...]] = ContextVar(
    "papilio_request_executions", default=()
)


def _has_source(request: AsyncContainer, source: AsyncContainer) -> bool:
    ancestor: AsyncContainer | None = request
    while ancestor is not None:
        if ancestor is source:
            return True
        ancestor = ancestor.parent_container
    return False


def _existing_request(source: AsyncContainer) -> AsyncContainer | None:
    for execution in reversed(_executions.get()):
        if execution.source is not source and not _has_source(
            execution.request, source
        ):
            continue
        if source is execution.request and (
            not execution.active
            or execution.owner is not asyncio.current_task()
        ):
            raise RuntimeError(
                "Request scope belongs to another or closed task"
            )
        if execution.active and execution.owner is asyncio.current_task():
            return execution.request
    return source if source.scope is Scope.REQUEST else None


def _method_container(
    args: tuple[Any, ...], kwargs: dict[str, Any]
) -> AsyncContainer:
    source = getattr(args[0], "container", None) if args else None
    if not isinstance(source, AsyncContainer):
        raise TypeError("@request_scoped needs self.container: AsyncContainer")
    if source.scope is not Scope.APP:
        raise ValueError("@request_scoped methods require the APP container")
    return source


@overload
def request_scoped[**P, R](
    function: Callable[P, Awaitable[R]],
) -> Callable[P, CoroutineType[Any, Any, R]]: ...


@overload
def request_scoped[**P, R](
    *, container_getter: ContainerGetter[AsyncContainer]
) -> Callable[
    [Callable[P, Awaitable[R]]], Callable[P, CoroutineType[Any, Any, R]]
]: ...


def request_scoped[**P, R](
    function: Callable[P, Awaitable[R]] | None = None,
    *,
    container_getter: ContainerGetter[AsyncContainer] | None = None,
) -> (
    Callable[P, CoroutineType[Any, Any, R]]
    | Callable[
        [Callable[P, Awaitable[R]]], Callable[P, CoroutineType[Any, Any, R]]
    ]
):
    """Run an async method in a short native REQUEST, without committing.

    Bare methods use the APP AsyncContainer in self.container. Inject() marks
    keyword-only dependencies while keeping ordinary argument/return typing.
    FromDishka annotations and explicit container_getter bindings also work.
    Same-task nested calls reuse the scope; independent tasks get their own.
    Only owned scopes are closed. Return materialized results, not cursors.
    """
    getter = container_getter or _method_container
    if function is None:
        return cast(
            Callable[
                [Callable[P, Awaitable[R]]],
                Callable[P, CoroutineType[Any, Any, R]],
            ],
            partial(request_scoped, container_getter=getter),
        )
    if not inspect.iscoroutinefunction(function):
        raise TypeError("@request_scoped requires an async function")
    if _SCOPE_ARG in inspect.signature(function).parameters:
        raise TypeError(f"Reserved injection parameter: {_SCOPE_ARG}")
    injected = cast(
        Callable[..., Awaitable[R]],
        wrap_injection(
            func=function,
            is_async=True,
            parse_dependency=_dependency,
            additional_params=[
                inspect.Parameter(_SCOPE_ARG, inspect.Parameter.KEYWORD_ONLY)
            ],
            container_getter=lambda args, kwargs: kwargs[_SCOPE_ARG],
        ),
    )

    @wraps(injected)
    async def invoke(*args: P.args, **kwargs: P.kwargs) -> R:
        source = getter(args, kwargs)
        request = _existing_request(source)
        if request is not None and request is not source:
            if injected is function:
                return await function(*args, **kwargs)
            return await injected(*args, **kwargs, _papilio_scope=request)
        scope = None if request is source else source(scope=Scope.REQUEST)
        request = source if scope is None else await scope.__aenter__()
        execution = _Execution(source, request, asyncio.current_task())
        token = _executions.set((*_executions.get(), execution))
        try:
            if injected is function:
                return await function(*args, **kwargs)
            return await injected(*args, **kwargs, _papilio_scope=request)
        finally:
            execution.active = False
            _executions.reset(token)
            if scope is not None:
                with CancelScope(shield=True):
                    await scope.__aexit__(*sys.exc_info())

    # Hide the carrier used by Dishka; inspect only once, at decoration.
    signature = inspect.signature(injected)
    invoke.__signature__ = signature.replace(  # type: ignore[attr-defined]
        parameters=[
            p for p in signature.parameters.values() if p.name != _SCOPE_ARG
        ]
    )
    return cast(Callable[P, CoroutineType[Any, Any, R]], invoke)
