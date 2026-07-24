"""Control-write command queue and the worker-side applier (N15, ADR-0012).

Every control write (start / cancel / send_event / fork) is handed to the worker
through an in-process :class:`CommandQueue` instead of being written by the HTTP
thread, so the worker stays the **single writer** (ADR-0012). The HTTP handler
enqueues a command and returns immediately (write-then-poll); the worker drains the
queue on each poll tick and applies each command against the store.

``CommandQueue`` is thread-safe (the API runs on its own thread, the worker on the
loop thread) using a stdlib lock — no asyncio primitive that would bind to one loop.
This module is pure Python: it imports the runner/engine lazily inside the applier so
``import satay.control`` never pulls the studio stack or forms an import cycle.
"""

from __future__ import annotations

import threading
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from satay.config import EffectSafety
from satay.journal import Store
from satay.journal.events import (
    TERMINAL_STATUSES,
    Event,
    EventType,
    InboxEventRecord,
    RunStatus,
)

if TYPE_CHECKING:
    from satay.testing.clock import Clock
    from satay.testing.faults import FaultInjector
    from satay.testing.rng import Rng


@dataclass(frozen=True, slots=True)
class StartRun:
    """Start a run: the worker resolves the workflow by name and drives it."""

    workflow_name: str
    workflow_input: Any = None
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    idempotency_key: str | None = None


@dataclass(frozen=True, slots=True)
class CancelRun:
    """Cancel a run: append ``WorkflowCancelled`` and halt (within one poll interval)."""

    run_id: str


@dataclass(frozen=True, slots=True)
class SendEvent:
    """Deliver an external event into the V3 inbox (one delivery path with the Python API)."""

    event_type: str
    key: str | None = None
    payload: Any = None
    run_id: str | None = None


#: The commands the worker applies. ``fork`` is validated + accepted at the HTTP layer
#: but its execution is deferred to V7, so it is not a queued command here.
Command = StartRun | CancelRun | SendEvent


class CommandQueue:
    """Thread-safe FIFO the API thread submits to and the worker drains (ADR-0012)."""

    def __init__(self) -> None:
        self._items: deque[Command] = deque()
        self._lock = threading.Lock()

    def submit(self, command: Command) -> None:
        """Enqueue a control-write command (thread-safe, non-blocking)."""
        with self._lock:
            self._items.append(command)

    def drain(self) -> list[Command]:
        """Atomically remove and return all pending commands in FIFO order."""
        with self._lock:
            items = list(self._items)
            self._items.clear()
        return items

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


async def append_cancellation(
    store: Store,
    run_id: str,
    *,
    now: datetime,
    injector: FaultInjector | None = None,
) -> bool:
    """Append ``WorkflowCancelled`` and set the run terminal; return whether it applied.

    Idempotent and shared by the HTTP ``cancel`` command and ``RunHandle.cancel()`` so
    both reach the *same* journal transition. A no-op for an unknown or already-terminal
    run (returns ``False``).
    """
    record = await store.get_run(run_id)
    if record is None or record.status in TERMINAL_STATUSES:
        return False
    stored = await store.append(Event(run_id=run_id, type=EventType.WORKFLOW_CANCELLED, ts=now))
    if injector is not None:
        await injector.reached(stored.type.value)
    await store.set_status(run_id, RunStatus.CANCELLED)
    return True


async def apply_command(
    command: Command,
    *,
    store: Store,
    clock: Clock | None = None,
    rng: Rng | None = None,
    injector: FaultInjector | None = None,
    effect_safety: EffectSafety = EffectSafety.WARN,
) -> None:
    """Apply one drained command against the store (the worker is the sole writer)."""
    from satay.journal.events import utc_now

    now = clock.now() if clock is not None else utc_now()

    if isinstance(command, CancelRun):
        await append_cancellation(store, command.run_id, now=now, injector=injector)
        return

    if isinstance(command, SendEvent):
        await store.add_inbox_event(
            InboxEventRecord(
                event_type=command.event_type,
                key=command.key,
                payload_ref=command.payload,
                received_at=now,
                run_id=command.run_id,
            )
        )
        return

    # StartRun: resolve the registered workflow and drive it through the same path the
    # in-process Python API uses, so HTTP-started and code-started runs are identical.
    from satay.api.registry import REGISTRY
    from satay.api.runner import build_run_handle

    workflow_def = REGISTRY.get_workflow(command.workflow_name)
    if workflow_def is None:
        raise UnknownWorkflowError(command.workflow_name)
    handle = build_run_handle(
        workflow_def.fn,
        command.workflow_input,
        run_id=command.run_id,
        idempotency_key=command.idempotency_key,
        store=store,
        injector=injector,
        clock=clock,
        rng=rng,
        effect_safety=effect_safety,
    )
    await handle.result()


class UnknownWorkflowError(LookupError):
    """Raised when a ``StartRun`` names a workflow absent from the registry (HTTP 400)."""

    def __init__(self, workflow_name: str) -> None:
        super().__init__(f"workflow {workflow_name!r} is not registered")
        self.workflow_name = workflow_name


class ForkValidationError(ValueError):
    """Raised when a fork request names a missing source run or fork-point (HTTP 400)."""


async def validate_fork_request(store: Store, source_run_id: str, fork_point_seq: int) -> None:
    """Validate a fork request's source run and fork-point event (N15, V5 stub).

    V5 only stands up and validates the route; creating the forked run and re-running
    its downstream is V7. Rejects an unknown source run and a fork-point ``seq`` that
    is not a real event on that run, so V7 builds on a stable, checked surface.
    """
    record = await store.get_run(source_run_id)
    if record is None:
        raise ForkValidationError(f"source run {source_run_id!r} not found")
    events = await store.read_events(source_run_id)
    seqs = {e.seq for e in events}
    if fork_point_seq not in seqs:
        raise ForkValidationError(
            f"fork-point seq {fork_point_seq} is not an event of run {source_run_id!r}"
        )


__all__ = [
    "CancelRun",
    "Command",
    "CommandQueue",
    "ForkValidationError",
    "SendEvent",
    "StartRun",
    "UnknownWorkflowError",
    "append_cancellation",
    "apply_command",
    "validate_fork_request",
]
