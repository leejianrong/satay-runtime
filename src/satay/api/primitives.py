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
    from satay.config import EffectSafety
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
    ``effect_safety`` overrides the project mode (``off``/``warn``/``strict``);
    unset resolves from ``SATAY_EFFECT_SAFETY`` then the ``warn`` dev default.
    """
    # Imported lazily: the runner pulls in the replay engine, and importing it at
    # module scope would form a cycle through ``satay.api``.
    from satay.api.runner import build_run_handle
    from satay.config import resolve_effect_safety

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
    the Python-API entry point; the HTTP ``send_event`` route lands in V5 and writes to
    the same inbox. ``store`` is the injectable test seam; it defaults to the
    project-local database.
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


async def map(
    task: Callable[..., Awaitable[Any]],
    items: Iterable[Any],
    *,
    key: Callable[[Any], str] | None = None,
) -> list[Any]:
    """Durable fan-out of ``task`` over ``items``; fail-fast (N5, D21, lands in V4)."""
    raise NotImplementedError("satay.map lands in V4")


async def gather(*awaitables: Awaitable[Any]) -> list[Any]:
    """Durably await several durable calls concurrently; fail-fast (N5, D21, lands in V4)."""
    raise NotImplementedError("satay.gather lands in V4")


async def start_child(
    workflow: Callable[..., Awaitable[Any]],
    workflow_input: Any = None,
    *,
    key: str | None = None,
) -> RunHandle:
    """Start a durable child workflow (N5, lands in V4)."""
    raise NotImplementedError("satay.start_child lands in V4")
