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

from satay.config import EffectSafety, NondeterminismPolicy, VersionMismatchPolicy
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


class _Inherit:
    """The type of :data:`INHERIT` — a sentinel distinct from every real input value."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "INHERIT"


#: The journal events that make a run terminal — a prefix containing one is a whole
#: finished run, which the replay engine short-circuits instead of re-executing.
_TERMINAL_EVENT_TYPES = frozenset(
    {EventType.WORKFLOW_COMPLETED, EventType.WORKFLOW_FAILED, EventType.WORKFLOW_CANCELLED}
)


#: "No input override": the fork rehydrates the source run's ``WorkflowCreated`` input.
#: A sentinel rather than ``None`` because ``None`` is a perfectly good workflow input,
#: so ``workflow_input=None`` must mean "run it with ``None``", not "inherit" (KAN-481).
INHERIT: Any = _Inherit()


@dataclass(frozen=True, slots=True)
class ForkRun:
    """Fork a terminal run from a journal point (N15, V7).

    The worker seeds ``run_id``'s journal from ``source_run_id``'s events up to and
    including ``fork_point_seq``, appends ``RunForked`` lineage, then drives it — the
    dropped downstream calls are journal misses in the new run, so they re-run under any
    changed code. The new ``run_id`` is allocated at enqueue time so the HTTP caller can
    return it immediately (write-then-poll, ADR-0012).

    ``workflow_input`` overrides the input the fork runs under (KAN-481, ADR-0028); left
    at :data:`INHERIT` the fork rehydrates the source's recorded input.
    """

    source_run_id: str
    fork_point_seq: int
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    workflow_input: Any = INHERIT


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
    nondeterminism: NondeterminismPolicy = NondeterminismPolicy.STRICT,
    version_mismatch: VersionMismatchPolicy = VersionMismatchPolicy.WARN,
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
            nondeterminism=nondeterminism,
            version_mismatch=version_mismatch,
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
        nondeterminism=nondeterminism,
        version_mismatch=version_mismatch,
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

    Kept as the explicit-seq entry point; :func:`resolve_fork_point` is the general form
    that also accepts ``before_task=`` and applies the same checks.
    """
    await resolve_fork_point(store, source_run_id, fork_point_seq=fork_point_seq)


async def _validate_source_run(store: Store, source_run_id: str) -> None:
    """Reject an unknown or non-terminal fork source (shared by both entry points)."""
    record = await store.get_run(source_run_id)
    if record is None:
        raise ForkValidationError(f"source run {source_run_id!r} not found")
    if record.status not in TERMINAL_STATUSES:
        raise ForkValidationError(
            f"cannot fork run {source_run_id!r}: its status is {record.status.value!r}; the MVP "
            f"forks only terminal runs (completed/failed/cancelled). Forking an "
            f"actively-executing run is not supported (ADR-0004/Q53)."
        )


async def resolve_fork_point(
    store: Store,
    source_run_id: str,
    *,
    fork_point_seq: int | None = None,
    before_task: str | None = None,
    before_ordinal: int | None = None,
) -> int:
    """Resolve a fork point from a task name, or validate an explicit seq (KAN-481).

    Choosing where to cut used to be journal archaeology — scanning ``TaskScheduled``
    events for a name, taking the ``min`` seq, then stepping back one event. This is
    that scan, once, in the runtime:

    - ``fork_point_seq=`` — the raw form, validated as before (a real event seq on a
      terminal source run).
    - ``before_task="synthesize"`` — cut so the fork's copied prefix ends immediately
      **before** that task was scheduled, so it re-runs. When the name was scheduled
      more than once this selects the **earliest** occurrence, deliberately: a fork
      point later than that would leave results from earlier occurrences in the prefix,
      recorded under exactly the code or input you are trying to change, and a
      half-updated run is a worse default than an over-complete one. Pick a specific
      occurrence with ``before_ordinal=``.
    - ``before_ordinal=N`` — with ``before_task``, select that task's Nth durable call
      (the ``ordinal`` half of the ``task:ordinal`` identity Studio and ``compare``
      show). Keyed fan-out items have no ordinal and are not selectable this way.

    Exactly one of ``fork_point_seq`` / ``before_task`` is required. A name that never
    ran raises :class:`ForkValidationError` listing the names that did, so the error
    tells the caller what to type instead.
    """
    await _validate_source_run(store, source_run_id)
    if (fork_point_seq is None) == (before_task is None):
        raise ForkValidationError(
            "pass exactly one of fork_point_seq= or before_task= to choose a fork point"
        )
    if before_ordinal is not None and before_task is None:
        raise ForkValidationError("before_ordinal= selects an occurrence of before_task=")

    events = await store.read_events(source_run_id)
    if fork_point_seq is not None:
        if fork_point_seq not in {e.seq for e in events}:
            raise ForkValidationError(
                f"fork-point seq {fork_point_seq} is not an event of run {source_run_id!r}"
            )
        return fork_point_seq

    scheduled = [
        e
        for e in events
        if e.type is EventType.TASK_SCHEDULED and e.payload.get("task_name") == before_task
    ]
    if not scheduled:
        ran = sorted(
            {str(e.payload.get("task_name")) for e in events if e.type is EventType.TASK_SCHEDULED}
        )
        detail = ", ".join(repr(name) for name in ran) if ran else "no tasks at all"
        raise ForkValidationError(
            f"run {source_run_id!r} never scheduled a task named {before_task!r}; it ran {detail}"
        )
    if before_ordinal is None:
        chosen = min(scheduled, key=lambda e: e.seq)
    else:
        matching = [e for e in scheduled if e.payload.get("ordinal") == before_ordinal]
        if not matching:
            available = sorted(str(e.payload.get("ordinal", "keyed")) for e in scheduled)
            raise ForkValidationError(
                f"task {before_task!r} has no ordinal {before_ordinal} in run "
                f"{source_run_id!r}; it ran with ordinals {', '.join(available)}"
            )
        chosen = min(matching, key=lambda e: e.seq)

    earlier = [e.seq for e in events if e.seq < chosen.seq]
    if not earlier:  # pragma: no cover - WorkflowCreated always precedes a TaskScheduled
        raise ForkValidationError(
            f"task {before_task!r} is the first event of run {source_run_id!r}; "
            f"there is no prefix to fork from"
        )
    return max(earlier)


async def create_fork(
    store: Store,
    *,
    source_run_id: str,
    fork_point_seq: int,
    new_run_id: str,
    now: datetime,
    code_version: str | None = None,
    workflow_input: Any = INHERIT,
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

    With ``workflow_input`` set (anything but :data:`INHERIT`) the copied
    ``WorkflowCreated`` carries the **new** input and ``RunForked`` gains
    ``input_overridden: true`` plus the source's ``source_input_ref`` (KAN-481,
    ADR-0028). The override is written into the fork's journal rather than passed at
    drive time so it is durable: a fork that parks on a timer and is woken later by the
    poll loop, or is resumed after a crash, reads its own recorded input and cannot
    silently revert to the source's.
    """
    from satay.journal.codec import encode
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
    override = not isinstance(workflow_input, _Inherit)
    if override and any(e.type in _TERMINAL_EVENT_TYPES for e in prefix):
        # The copied prefix is a whole finished run, so the engine's idempotent-terminal
        # short-circuit fires and nothing re-executes — the new input would be recorded
        # and then silently ignored. Refuse rather than hand back the old answer under a
        # new input (ADR-0028; the same reasoning as ADR-0022's silent-wrong-answer).
        raise ForkValidationError(
            f"fork point {fork_point_seq} copies run {source_run_id!r} through its terminal "
            f"event, so nothing would re-execute and workflow_input= would have no effect; "
            f"choose an earlier fork point (before_task= is the easy way)"
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
    lineage: dict[str, Any] = {"source_run_id": source_run_id, "fork_point_seq": fork_point_seq}
    for event in prefix:
        payload = dict(event.payload)
        if override and event.type is EventType.WORKFLOW_CREATED:
            lineage["input_overridden"] = True
            lineage["source_input_ref"] = payload.get("input_ref")
            payload["input_ref"] = encode(workflow_input)
        await store.append(Event(run_id=new_run_id, type=event.type, payload=payload, ts=event.ts))
    if override and "input_overridden" not in lineage:  # pragma: no cover - defensive
        raise ForkValidationError(
            f"run {source_run_id!r} has no WorkflowCreated event to override the input on"
        )
    await store.append(Event(run_id=new_run_id, type=EventType.RUN_FORKED, payload=lineage, ts=now))
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
    nondeterminism: NondeterminismPolicy = NondeterminismPolicy.STRICT,
    version_mismatch: VersionMismatchPolicy = VersionMismatchPolicy.WARN,
) -> None:
    """Seed the forked run then drive it, re-running the dropped downstream (N15).

    Drives through the replay engine directly (not the resume path) so a fresh fork
    records **no** ``WorkflowResumed`` and carries no ⚡ — it is a new run, not a crash
    recovery. Replay reuses the copied completions as journal hits and re-executes the
    calls after the fork point as misses, picking up any changed task implementation.
    """
    workflow_name = await create_fork(
        store,
        source_run_id=command.source_run_id,
        fork_point_seq=command.fork_point_seq,
        new_run_id=command.run_id,
        now=now,
        workflow_input=command.workflow_input,
    )
    await drive_forked_run(
        store,
        command.run_id,
        workflow_name=workflow_name,
        clock=clock,
        rng=rng,
        injector=injector,
        effect_safety=effect_safety,
        nondeterminism=nondeterminism,
        version_mismatch=version_mismatch,
    )


async def drive_forked_run(
    store: Store,
    run_id: str,
    *,
    workflow_name: str,
    clock: Clock | None = None,
    rng: Rng | None = None,
    injector: FaultInjector | None = None,
    effect_safety: EffectSafety = EffectSafety.WARN,
    nondeterminism: NondeterminismPolicy = NondeterminismPolicy.STRICT,
    version_mismatch: VersionMismatchPolicy = VersionMismatchPolicy.WARN,
) -> None:
    """Drive an already-seeded fork off its **own** recorded input (N15, KAN-481).

    The input comes from the fork's journal, never from the caller, so an overridden
    input survives a park-and-wake or a crash-and-resume (see :func:`create_fork`).
    Shared by the worker's :func:`apply_fork` and the in-process ``satay.fork`` handle,
    so a fork driven from code and one driven from Studio take the identical path.
    """
    from satay.api.registry import REGISTRY
    from satay.journal.codec import rehydrate
    from satay.replay.engine import ReplayEngine

    workflow_def = REGISTRY.get_workflow(workflow_name)
    if workflow_def is None:
        raise UnknownWorkflowError(workflow_name)

    workflow_input = None
    for event in await store.read_events(run_id):
        if event.type is EventType.WORKFLOW_CREATED:
            workflow_input = rehydrate(
                event.payload.get("input_ref"), _input_annotation(workflow_def.fn)
            )
            break

    engine = ReplayEngine(
        store=store,
        run_id=run_id,
        injector=injector,
        clock=clock,
        rng=rng,
        effect_safety=effect_safety,
        nondeterminism=nondeterminism,
        version_mismatch=version_mismatch,
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
    "INHERIT",
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
    "drive_forked_run",
    "resolve_fork_point",
    "validate_fork_request",
]
