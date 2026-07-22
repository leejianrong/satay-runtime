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
) -> Callable[[_AsyncFn], _AsyncFn]:
    """Register a task and wrap calls as durable calls (N2).

    ``retries``/``timeout``/``side_effect`` are recorded on the definition now; the
    retry loop and effect-safety enforcement land in V2 (single attempt in V1).
    """

    def decorator(fn: _AsyncFn) -> _AsyncFn:
        definition = TaskDefinition(
            name=fn.__name__,
            fn=fn,
            retries=retries,
            timeout=timeout,
            side_effect=side_effect,
        )
        REGISTRY.register_task(definition)

        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            driver = CURRENT_DRIVER.get()
            if driver is None:
                # Outside a workflow drive: execute inline (tasks stay callable).
                return await fn(*args, **kwargs)
            return await driver.durable_call(definition, args, kwargs)

        setattr(wrapper, TASK_ATTR, definition)
        return cast("_AsyncFn", wrapper)

    return decorator
