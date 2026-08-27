"""Author-facing decorators: ``@satay.workflow`` and ``@satay.task`` (N1/N2).

Pure Python, no third-party dependency (ARCHITECTURE §3.1). ``@satay.workflow``
registers the coroutine and returns it annotated with its definition so
:func:`satay.start` can resolve name and callable. ``@satay.task`` registers the
task and returns a wrapper whose call, *inside a running workflow*, becomes a durable
call routed to the replay engine (via the current-drive ``ContextVar``) rather than
executing inline. Called outside a drive, a task simply executes — so tasks stay
independently callable and testable.

``@satay.workflow`` also validates the signature it is handed, at decoration time
(KAN-579). See :func:`_validate_workflow_signature` for why that check is worth having
and why the equivalent does not exist for ``@satay.task``.
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar, cast

from satay.api.registry import REGISTRY, TaskDefinition, WorkflowDefinition
from satay.replay.driver import CURRENT_DRIVER

_AsyncFn = TypeVar("_AsyncFn", bound=Callable[..., Awaitable[Any]])

#: Attribute stamped on a decorated callable, carrying its registry definition.
WORKFLOW_ATTR = "__satay_workflow__"
TASK_ATTR = "__satay_task__"


def _validate_workflow_signature(fn: Callable[..., Awaitable[Any]]) -> None:
    """Reject a workflow the runtime could never call, while the traceback still helps.

    The runtime invokes a workflow in exactly one way — ``await workflow_def.fn(
    workflow_input)`` in :meth:`satay.replay.engine.ReplayEngine.drive` — with a single
    positional argument, ``None`` when :func:`satay.start` was given no input. Every entry
    point funnels through that one call: in-process start, child workflows, forks, and the
    HTTP control plane.

    So a zero-parameter (or keyword-only, or two-required-parameter) workflow is not a
    style question, it is uncallable. Left unchecked it fails at *drive* time, which is
    considerably worse than it sounds: the ``TypeError`` is raised inside the engine's
    drive, so it is caught by the generic failure handler, **durably recorded as a
    ``WorkflowFailed`` event**, and re-raised to the author as a
    :class:`~satay.api.run_handle.WorkflowFailedError` wrapping a *stringified* traceback.
    An authoring typo thereby leaves a permanent junk run in an append-only journal
    (ADR-0004, which has no deletion) and presents itself as a runtime failure. Checked
    here, it is a ``TypeError`` at import, pointing at the ``def``.

    The predicate is a real bind against the real signature rather than a parameter count,
    so it accepts every shape the runtime accepts — ``(x)``, ``(x: int)``, ``(x=5)``,
    ``(_: Any = None)``, ``(x, /)``, ``(a, b=2)``, ``(*args)`` — and rejects exactly those
    it does not.

    ``@satay.task`` deliberately gets no equivalent: a task is called with the author's own
    arguments, forwarded verbatim, so its legal arity is "whatever the caller passes" and
    any rule here would be a false positive by construction. Zero-parameter and
    multi-parameter tasks are both in use in this repo, and both are correct.
    """
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError):  # pragma: no cover - exotic/builtin callable
        # No signature to inspect (a C builtin, say). The runtime's own call is still the
        # authority; refusing to guess is better than a false rejection.
        return

    try:
        signature.bind(None)
    except TypeError as exc:
        raise TypeError(
            f"@satay.workflow {fn.__name__}{signature} cannot be called with one "
            f"argument ({exc}). Satay drives a workflow as "
            f"{fn.__name__}(workflow_input), passing the input given to satay.start() "
            f"(None when it was given none), so a workflow must accept the input as "
            f"exactly one positional parameter. Add one — use `_: Any = None` if this "
            f"workflow does not need the input."
        ) from None

    # Async-only is a runtime-wide rule, not a preference (ADR-0007), and a plain `def`
    # here fails in the same journal-polluting way a bad arity does: the engine awaits the
    # return value and records the resulting TypeError as a WorkflowFailed event. Limited
    # to plain functions and methods on purpose — a callable object whose `__call__` is
    # async is not a coroutine function by this test, and rejecting it would be the one
    # false positive available here.
    if (inspect.isfunction(fn) or inspect.ismethod(fn)) and not inspect.iscoroutinefunction(fn):
        raise TypeError(
            f"@satay.workflow {fn.__name__} must be an `async def`. Satay awaits a "
            f"workflow's return value, and workflows and tasks are async-only."
        )


def workflow[AsyncFnT: Callable[..., Awaitable[Any]]](fn: AsyncFnT) -> AsyncFnT:
    """Register a workflow definition and return it, annotated for the runtime (N1).

    Raises :class:`TypeError` at decoration time for a signature the runtime could never
    call — see :func:`_validate_workflow_signature` (KAN-579).
    """
    _validate_workflow_signature(fn)
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

    **``idempotent=True`` is a promise about one run, not about the work** (KAN-476).
    It says the body derives its dedupe key from ``ctx.idempotency_key``, which is
    ``sha256(run_id, task_name, ordinal-or-map-key)`` — so it holds across retries and
    across a crash-and-resume of *this* run, and stops there. Two silent gaps come with
    it, both documented in full on :attr:`satay.TaskContext.idempotency_key`:

    - **A re-trigger is not deduplicated.** Starting the same logical work again mints a
      new ``run_id``, hence new keys, hence a second copy of the effect. Compose the
      declaration with a keyed start — ``satay.start(wf, x, idempotency_key=...)`` — so
      the repeat resolves to the same run instead of a new one. Satay warns when a
      task declared this way runs in a run with no start-level key of its own.
    - **One key covers one call, not one row.** A body writing N rows must compose
      ``f"{ctx.idempotency_key}#{row_id}"`` per row. Using the bare key as a unique
      column writes the first row and silently ignores the rest. Nothing in the runtime
      can see that, so nothing warns.

    Declaring ``idempotent=True`` without keeping the promise is worse than not
    declaring it: it turns the unguarded-effect warning off.
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
