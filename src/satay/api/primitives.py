"""The durable primitives and ``satay.start``.

Every function here is a durable call routed through the replay engine, except
``start`` (creates/looks up a run) and ``send_event`` (a control-plane write). All
are public surface; behaviour lands in the slice noted on each.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from satay.api.run_handle import RunHandle
from satay.journal.codec import encode
from satay.replay.driver import CURRENT_DRIVER

if TYPE_CHECKING:
    from satay.config import EffectSafety, NondeterminismPolicy, VersionMismatchPolicy
    from satay.journal import Store
    from satay.testing.clock import Clock
    from satay.testing.faults import FaultInjector
    from satay.testing.rng import Rng


def start(
    workflow: Callable[..., Awaitable[Any]],
    workflow_input: Any = None,
    *,
    idempotency_key: str | None = None,
    run_id: str | None = None,
    store: Store | None = None,
    injector: FaultInjector | None = None,
    clock: Clock | None = None,
    rng: Rng | None = None,
    effect_safety: str | EffectSafety | None = None,
    nondeterminism: str | NondeterminismPolicy | None = None,
    version_mismatch: str | VersionMismatchPolicy | None = None,
) -> RunHandle:
    """Create or look up a run and return its handle (N3).

    New run (no ``run_id`` or an unknown one): allocate a stable ``run_id``, record
    ``WorkflowCreated``, and drive on ``await handle.result()``. Resume: pass the
    ``run_id`` of an existing **non-terminal** run to re-drive it. Keyed idempotent
    start (N13): pass ``idempotency_key=`` (without a ``run_id``) and a repeated key
    resolves to the same logical run instead of creating a duplicate.

    A terminal run (by id or key) is a no-op that returns the recorded result.

    ``store`` / ``injector`` / ``clock`` / ``rng`` are the injectable test seam
    (ADR-0011); ``store`` defaults to the project-local ``./.satay`` SQLite database.
    ``effect_safety`` overrides the unguarded-side-effect mode (``off``/``warn``/
    ``strict``); unset resolves from ``SATAY_EFFECT_SAFETY`` then the ``warn`` dev
    default. ``nondeterminism`` overrides the **replay-divergence** policy (same three
    values); unset resolves from ``SATAY_NONDETERMINISM`` then the ``strict`` default,
    so a divergent replay raises unless you opt out (ADR-0022). ``version_mismatch``
    overrides the **code-version mismatch on resume** policy (same three values); unset
    resolves from ``SATAY_VERSION_MISMATCH`` then the ``warn`` default (ADR-0023).

    Those three settings are independent: turning one off does not quiet the others.
    """
    # Imported lazily: the runner pulls in the replay engine, and importing it at
    # module scope would form a cycle through ``satay.api``.
    from satay.api.runner import build_run_handle
    from satay.config import (
        resolve_effect_safety,
        resolve_nondeterminism,
        resolve_version_mismatch,
    )

    resolved_store = store if store is not None else _default_store()
    return build_run_handle(
        workflow,
        workflow_input,
        run_id=run_id,
        idempotency_key=idempotency_key,
        store=resolved_store,
        injector=injector,
        clock=clock,
        rng=rng,
        effect_safety=resolve_effect_safety(effect_safety),
        nondeterminism=resolve_nondeterminism(nondeterminism),
        version_mismatch=resolve_version_mismatch(version_mismatch),
    )


def _default_store() -> Store:
    from satay.config import db_path, resolve_data_dir
    from satay.journal.store import SQLiteStore

    data_dir = resolve_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    return SQLiteStore.open(db_path(data_dir))


def _as_timedelta(duration: float | timedelta) -> timedelta:
    """Coerce a ``float`` (seconds) or ``timedelta`` to a ``timedelta``."""
    return duration if isinstance(duration, timedelta) else timedelta(seconds=duration)


def event_type_name(event_type: type[Any] | str) -> str:
    """The stable inbox key for an event type: ``module.qualname`` (or a string as-is).

    ``wait_for_event(Type)`` and ``send_event(event=Type(...))`` must derive the *same*
    string so a wait and a send match by ``(event_type, key)`` (V3 design rule 3).
    """
    if isinstance(event_type, str):
        return event_type
    return f"{event_type.__module__}.{event_type.__qualname__}"


async def sleep(duration: float | timedelta) -> None:
    """Durably sleep for ``duration`` (N5).

    A durable call: on the first miss it records a timer and parks the run (releasing it
    from memory); it resumes when the worker fires the timer. Survives a crash because
    the timer row and journal are durable. Must be called inside a running workflow.
    """
    driver = CURRENT_DRIVER.get()
    if driver is None:
        raise RuntimeError("satay.sleep() must be called inside a @satay.workflow body")
    await driver.durable_sleep(_as_timedelta(duration))


async def wait_for_event(
    event_type: type[Any] | str,
    *,
    key: str | None = None,
    timeout: float | timedelta | None = None,
) -> Any:
    """Durably wait for an external event of ``event_type``, matched by ``key`` (N5).

    Matches an inbox event by ``(event_type, key)`` — an event delivered *before* the
    wait is still matched. With a ``timeout`` the wait resolves to ``None`` if no event
    arrives by then (a delivered event always wins a simultaneously-due timeout,
    ADR-0021). Returns the delivered event, rehydrated to ``event_type`` when a class is
    given. Must be called inside a running workflow.
    """
    driver = CURRENT_DRIVER.get()
    if driver is None:
        raise RuntimeError("satay.wait_for_event() must be called inside a @satay.workflow body")
    annotation = None if isinstance(event_type, str) else event_type
    return await driver.durable_wait_for_event(
        event_type_name(event_type),
        key,
        None if timeout is None else _as_timedelta(timeout),
        annotation,
    )


async def send_event(
    event: Any,
    *,
    key: str | None = None,
    run_id: str | None = None,
    store: Store | None = None,
) -> None:
    """Deliver an external ``event`` to the inbox (control-plane write, N5).

    The event is encoded via the V1 codec and buffered in the inbox keyed by
    ``(type(event), key)``; the poll loop delivers it to a run waiting on that pair, or
    it waits in the inbox until matched (an event may arrive before the wait). This is
    the Python-API entry point; the HTTP route ``POST /runs/{run_id}/events`` writes to the
    same inbox. ``store`` is the injectable test seam; it defaults to the project-local
    database.
    """
    from satay.journal.events import InboxEventRecord, utc_now

    resolved_store = store if store is not None else _default_store()
    await resolved_store.add_inbox_event(
        InboxEventRecord(
            event_type=event_type_name(type(event)),
            key=key,
            payload_ref=encode(event),
            received_at=utc_now(),
            run_id=run_id,
        )
    )


#: In-flight bound for ``satay.map`` when ``concurrency=`` is unspecified.
DEFAULT_MAP_CONCURRENCY = 8


async def map(
    task: Callable[..., Awaitable[Any]],
    items: Iterable[Any],
    *,
    key: Callable[[Any], str] | None = None,
    concurrency: int = DEFAULT_MAP_CONCURRENCY,
    return_exceptions: bool = False,
) -> list[Any]:
    """Durable fan-out of ``task`` over ``items``, keyed by ``key=`` (N5, A6.1).

    Each item is a keyed durable call ``(task_name, key(item))`` that independently
    consults the journal, so on resume mid-fan-out completed items are reused and only
    unresolved items re-run (the signature demo). Up to ``concurrency`` items run at once
    on the asyncio loop; results rejoin in **input order** regardless of completion order.
    ``key=`` is required and must return a unique, stable, non-empty string per item
    (ADR-0002); a missing or duplicate key is a usage error raised at schedule time.

    **Failure.** Fail-fast by default (ADR-0020): a failed item raises through the
    ``map``, in-flight siblings settle but their results are discarded. Pass
    ``return_exceptions=True`` for **collect mode** (ADR-0027): every item settles, the
    returned list holds each item's result *or* a ``satay.TaskFailedError`` in its input
    position, and each failure is recorded in the journal as its own terminal
    ``TaskFailed`` event — so the runtime still sees the failure while the fan-out
    survives. Collected errors are always ``TaskFailedError``, never the task's own
    exception class, so the value is identical on replay.

    Must be called inside a running workflow.
    """
    driver = CURRENT_DRIVER.get()
    if driver is None:
        raise RuntimeError("satay.map() must be called inside a @satay.workflow body")
    return await driver.durable_map(_resolve_task(task), items, key, concurrency, return_exceptions)


async def gather(*awaitables: Awaitable[Any], return_exceptions: bool = False) -> list[Any]:
    """Durably await several durable calls concurrently (N5, A6.1).

    Awaits heterogeneous durable calls together — task calls, nested ``map`` calls, and
    ``start_child`` calls (whose returned handle is resolved to the child's result) — each
    keeping its own identity, and rejoins results **positionally** in argument order.

    **Failure.** Fail-fast by default (ADR-0020): a single failed member fails the whole
    ``gather``. With ``return_exceptions=True`` (collect mode, ADR-0027) every member
    settles and a failing slot holds the error it raised — ``satay.TaskFailedError`` for a
    task (recorded as a terminal ``TaskFailed`` event), ``satay.WorkflowFailedError`` for
    a child run (already terminal in the child's own journal).

    Must be called inside a running workflow.
    """
    driver = CURRENT_DRIVER.get()
    if driver is None:
        raise RuntimeError("satay.gather() must be called inside a @satay.workflow body")
    return await driver.durable_gather(awaitables, return_exceptions)


async def start_child(
    workflow: Callable[..., Awaitable[Any]],
    workflow_input: Any = None,
    *,
    key: str | None = None,
) -> RunHandle:
    """Start a durable child workflow linked to the current run (N5, A6.2).

    Creates a full child run with its own journal, linked to its parent (the parent
    records ``ChildWorkflowScheduled``; the child records ``parent_run_id`` + the
    originating call identity). Returns a :class:`RunHandle` to the child — ``await
    handle.result()`` yields its result, reused as a durable-call hit on parent replay.
    A child crashed mid-flight resumes (not restarts) on parent resume; a failed child
    raises (fail-fast, ADR-0020). ``key=`` gives the child call an explicit stable
    identity (otherwise it is identified by call ordinal). Must be called inside a
    running workflow.
    """
    driver = CURRENT_DRIVER.get()
    if driver is None:
        raise RuntimeError("satay.start_child() must be called inside a @satay.workflow body")
    return await driver.durable_child(_resolve_workflow_def(workflow), workflow_input, key)


def _resolve_task(task: Callable[..., Awaitable[Any]]) -> Any:
    """Resolve a ``@satay.task``-decorated callable to its registered definition."""
    from satay.api.decorators import TASK_ATTR
    from satay.api.registry import TaskDefinition

    definition = getattr(task, TASK_ATTR, None)
    if isinstance(definition, TaskDefinition):
        return definition
    raise TypeError(
        f"{getattr(task, '__name__', task)!r} is not a @satay.task; decorate it with "
        f"@satay.task before passing it to satay.map"
    )


def _resolve_workflow_def(workflow: Callable[..., Awaitable[Any]]) -> Any:
    """Resolve a ``@satay.workflow``-decorated callable to its registered definition."""
    from satay.api.runner import _resolve_workflow

    return _resolve_workflow(workflow)
