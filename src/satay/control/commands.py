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
    RunRecord,
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


@dataclass(frozen=True, slots=True)
class ForkRun:
    """Fork a terminal run from a journal point (N15, V7).

    The worker seeds ``run_id``'s journal from ``source_run_id``'s events up to and
    including ``fork_point_seq``, appends ``RunForked`` lineage, then drives it — the
    dropped downstream calls are journal misses in the new run, so they re-run under any
    changed code. The new ``run_id`` is allocated at enqueue time so the HTTP caller can
    return it immediately (write-then-poll, ADR-0012).
    """

    source_run_id: str
    fork_point_seq: int
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex)


#: The commands the worker applies as the single writer (ADR-0012). V7 adds ``ForkRun``:
#: the fork route validates synchronously then enqueues one, and the worker executes it.
Command = StartRun | CancelRun | SendEvent | ForkRun


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

    if isinstance(command, ForkRun):
        await apply_fork(
            command,
            store=store,
            now=now,
            clock=clock,
            rng=rng,
            injector=injector,
            effect_safety=effect_safety,
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
    """Validate a fork request's source run, status, and fork-point event (N15).

    Rejects (as an HTTP 400 at the route): an unknown source run; a **non-terminal**
    source run, with an error naming its status (the MVP forks only ``completed`` /
    ``failed`` / ``cancelled`` runs — forking a still-executing run adds a fork-point
    race with no MVP payoff, ADR-0004/Q53; the guard is a status allow-list so widening
    it to quiescent ``waiting`` runs is a one-line change later); and a ``fork_point_seq``
    that is not a real event ``seq`` on that run. Validated synchronously so the caller
    fails fast before anything is enqueued.
    """
    record = await store.get_run(source_run_id)
    if record is None:
        raise ForkValidationError(f"source run {source_run_id!r} not found")
    if record.status not in TERMINAL_STATUSES:
        raise ForkValidationError(
            f"cannot fork run {source_run_id!r}: its status is {record.status.value!r}; the MVP "
            f"forks only terminal runs (completed/failed/cancelled). Forking an "
            f"actively-executing run is not supported (ADR-0004/Q53)."
        )
    events = await store.read_events(source_run_id)
    seqs = {e.seq for e in events}
    if fork_point_seq not in seqs:
        raise ForkValidationError(
            f"fork-point seq {fork_point_seq} is not an event of run {source_run_id!r}"
        )


async def create_fork(
    store: Store,
    *,
    source_run_id: str,
    fork_point_seq: int,
    new_run_id: str,
    now: datetime,
    code_version: str | None = None,
) -> str:
    """Seed ``new_run_id``'s journal from the source prefix and record lineage (N15).

    Copies every source event with ``seq <= fork_point_seq`` **verbatim** (same type,
    payload, and timestamp; a fresh ``event_id`` and re-allocated per-run ``seq``) into
    the new run, then appends a ``RunForked`` event recording ``source_run_id`` and
    ``fork_point_seq``. The new run's record is stamped with the **current** code
    version (it is a new run created now, and the whole point is to re-run under changed
    code); any spilled-blob payload is *referenced*, never copied, so the source stays
    byte-for-byte unchanged (blobs are immutable, ADR-0004/Q54). Returns the source
    workflow name. Does **not** drive — :func:`apply_fork` drives it.
    """
    from satay.versioning import current_code_version

    record = await store.get_run(source_run_id)
    if record is None:  # pragma: no cover - guarded by validate_fork_request at the route
        raise ForkValidationError(f"source run {source_run_id!r} not found")
    events = await store.read_events(source_run_id)
    prefix = [e for e in events if e.seq <= fork_point_seq]
    if not prefix:
        raise ForkValidationError(
            f"fork-point seq {fork_point_seq} leaves nothing to seed from run {source_run_id!r}"
        )
    version = code_version if code_version is not None else current_code_version()
    await store.create_run(
        RunRecord(
            run_id=new_run_id,
            workflow_name=record.workflow_name,
            status=RunStatus.RUNNING,
            code_version=version,
            created_at=now,
            idempotency_key=None,
        )
    )
    for event in prefix:
        await store.append(
            Event(run_id=new_run_id, type=event.type, payload=dict(event.payload), ts=event.ts)
        )
    await store.append(
        Event(
            run_id=new_run_id,
            type=EventType.RUN_FORKED,
            payload={"source_run_id": source_run_id, "fork_point_seq": fork_point_seq},
            ts=now,
        )
    )
    return record.workflow_name


async def apply_fork(
    command: ForkRun,
    *,
    store: Store,
    now: datetime,
    clock: Clock | None = None,
    rng: Rng | None = None,
    injector: FaultInjector | None = None,
    effect_safety: EffectSafety = EffectSafety.WARN,
) -> None:
    """Seed the forked run then drive it, re-running the dropped downstream (N15).

    Drives through the replay engine directly (not the resume path) so a fresh fork
    records **no** ``WorkflowResumed`` and carries no ⚡ — it is a new run, not a crash
    recovery. Replay reuses the copied completions as journal hits and re-executes the
    calls after the fork point as misses, picking up any changed task implementation.
    """
    from satay.api.registry import REGISTRY
    from satay.journal.codec import rehydrate
    from satay.replay.engine import ReplayEngine

    workflow_name = await create_fork(
        store,
        source_run_id=command.source_run_id,
        fork_point_seq=command.fork_point_seq,
        new_run_id=command.run_id,
        now=now,
    )
    workflow_def = REGISTRY.get_workflow(workflow_name)
    if workflow_def is None:
        raise UnknownWorkflowError(workflow_name)

    workflow_input = None
    for event in await store.read_events(command.run_id):
        if event.type is EventType.WORKFLOW_CREATED:
            workflow_input = rehydrate(
                event.payload.get("input_ref"), _input_annotation(workflow_def.fn)
            )
            break

    engine = ReplayEngine(
        store=store,
        run_id=command.run_id,
        injector=injector,
        clock=clock,
        rng=rng,
        effect_safety=effect_safety,
    )
    await engine.drive(workflow_def, workflow_input)


def _input_annotation(fn: Any) -> Any:
    """Best-effort resolved annotation of ``fn``'s first parameter (``None`` if absent)."""
    import inspect
    from typing import get_type_hints

    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):  # pragma: no cover - builtins without signatures
        return None
    params = list(sig.parameters.values())
    if not params:
        return None
    try:
        hints = get_type_hints(fn)
    except Exception:
        ann = params[0].annotation
        return None if ann is inspect.Parameter.empty else ann
    return hints.get(params[0].name)


__all__ = [
    "CancelRun",
    "Command",
    "CommandQueue",
    "ForkRun",
    "ForkValidationError",
    "SendEvent",
    "StartRun",
    "UnknownWorkflowError",
    "append_cancellation",
    "apply_command",
    "apply_fork",
    "create_fork",
    "validate_fork_request",
]
