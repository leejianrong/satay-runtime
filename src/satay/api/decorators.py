"""Author-facing decorators: ``@satay.workflow`` and ``@satay.task`` (N1/N2).

Pure Python, no third-party dependency (ARCHITECTURE §3.1). ``@satay.workflow``
registers the coroutine and returns it annotated with its definition so
:func:`satay.start` can resolve name and callable. ``@satay.task`` registers the
task and returns a wrapper whose call, *inside a running workflow*, becomes a durable
call routed to the replay engine (via the current-drive ``ContextVar``) rather than
executing inline. Called outside a drive, a task simply executes — so tasks stay
independently callable and testable.
"""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar, cast

from satay.api.registry import REGISTRY, TaskDefinition, WorkflowDefinition
from satay.replay.driver import CURRENT_DRIVER

_AsyncFn = TypeVar("_AsyncFn", bound=Callable[..., Awaitable[Any]])

#: Attribute stamped on a decorated callable, carrying its registry definition.
WORKFLOW_ATTR = "__satay_workflow__"
TASK_ATTR = "__satay_task__"


def workflow[AsyncFnT: Callable[..., Awaitable[Any]]](fn: AsyncFnT) -> AsyncFnT:
    """Register a workflow definition and return it, annotated for the runtime (N1)."""
    definition = WorkflowDefinition(name=fn.__name__, fn=fn)
    REGISTRY.register_workflow(definition)
    setattr(fn, WORKFLOW_ATTR, definition)
    return fn


def task(
    *,
    retries: int = 0,
    timeout: float | None = None,
    side_effect: bool = False,
    idempotent: bool = False,
) -> Callable[[_AsyncFn], _AsyncFn]:
    """Register a task and wrap calls as durable calls (N2).

    ``retries``/``timeout`` drive the retry loop with exponential backoff (N10).
    ``side_effect``/``idempotent`` drive effect-safety enforcement (A10.2): a
    retryable side-effecting task must set ``idempotent=True`` (a promise it keys its
    effect on ``ctx.idempotency_key``), else ``effect_safety=strict`` rejects it at
    schedule time.
    """

    def decorator(fn: _AsyncFn) -> _AsyncFn:
        definition = TaskDefinition(
            name=fn.__name__,
            fn=fn,
            retries=retries,
            timeout=timeout,
            side_effect=side_effect,
            idempotent=idempotent,
        )
        REGISTRY.register_task(definition)

        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            driver = CURRENT_DRIVER.get()
            if driver is None:
                # Outside a workflow drive: execute inline (tasks stay callable) with a
                # detached context so ``task_context()`` still works.
                from satay.api.context import CURRENT_TASK_CONTEXT
                from satay.executor import detached_context

                token = CURRENT_TASK_CONTEXT.set(detached_context(definition.name))
                try:
                    return await fn(*args, **kwargs)
                finally:
                    CURRENT_TASK_CONTEXT.reset(token)
            return await driver.durable_call(definition, args, kwargs)

        setattr(wrapper, TASK_ATTR, definition)
        return cast("_AsyncFn", wrapper)

    return decorator
