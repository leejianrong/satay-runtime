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

if TYPE_CHECKING:
    from satay.journal import Store
    from satay.testing.clock import Clock
    from satay.testing.faults import FaultInjector


def start(
    workflow: Callable[..., Awaitable[Any]],
    workflow_input: Any = None,
    *,
    idempotency_key: str | None = None,
    run_id: str | None = None,
    store: Store | None = None,
    injector: FaultInjector | None = None,
    clock: Clock | None = None,
) -> RunHandle:
    """Create or look up a run and return its handle (N3).

    New run (no ``run_id`` or an unknown one): allocate a stable ``run_id``, record
    ``WorkflowCreated``, and drive on ``await handle.result()``. Resume: pass the
    ``run_id`` of an existing **non-terminal** run to re-drive it (the V1 crash-recovery
    mechanism — append-only keyed idempotent look-up, N13, is deferred to V2). A
    terminal ``run_id`` is a no-op that returns the recorded result.

    ``store`` / ``injector`` / ``clock`` are the injectable test seam (ADR-0011);
    ``store`` defaults to the project-local ``./.satay`` SQLite database.
    """
    # Imported lazily: the runner pulls in the replay engine, and importing it at
    # module scope would form a cycle through ``satay.api``.
    from satay.api.runner import build_run_handle

    resolved_store = store if store is not None else _default_store()
    return build_run_handle(
        workflow,
        workflow_input,
        run_id=run_id,
        idempotency_key=idempotency_key,
        store=resolved_store,
        injector=injector,
        clock=clock,
    )


def _default_store() -> Store:
    from satay.config import db_path, resolve_data_dir
    from satay.journal.store import SQLiteStore

    data_dir = resolve_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    return SQLiteStore.open(db_path(data_dir))


async def sleep(duration: float | timedelta) -> None:
    """Durably sleep for ``duration`` (N5, lands in V3)."""
    raise NotImplementedError("satay.sleep lands in V3")


async def wait_for_event(
    name: str,
    *,
    timeout: float | timedelta | None = None,
) -> Any:
    """Durably wait for a named external event, optionally with a timeout (N5, lands in V3)."""
    raise NotImplementedError("satay.wait_for_event lands in V3")


def send_event(run_id: str, name: str, payload: Any = None) -> None:
    """Deliver a named external event to a run (control-plane write, lands in V3)."""
    raise NotImplementedError("satay.send_event lands in V3")


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
