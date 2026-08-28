"""Journal event model — the envelope and the V1 payload types (A3.1, ADR-0004).

Events are stdlib **frozen dataclasses** (ADR-0016: no Pydantic in the core). Every
event shares the :class:`Event` envelope; the per-type payload is a plain ``dict`` so
the SQLite ``payload_json`` column stays schema-stable as new event types land in
later slices. The V1 subset is a strict prefix of the full ADR-0004 event set.

Payloads carry values behind ``input_ref`` / ``output_ref`` indirection. In V1 the
"ref" *is* the inlined encoded value (blob spill is V8), so V8 can swap a reference
in without a schema change.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class EventType(StrEnum):
    """The journal event types active through V4 (a prefix of ADR-0004's full set)."""

    WORKFLOW_CREATED = "WorkflowCreated"
    WORKFLOW_RESUMED = "WorkflowResumed"
    TASK_SCHEDULED = "TaskScheduled"
    TASK_ATTEMPT_STARTED = "TaskAttemptStarted"
    TASK_ATTEMPT_FAILED = "TaskAttemptFailed"
    TASK_COMPLETED = "TaskCompleted"
    WORKFLOW_COMPLETED = "WorkflowCompleted"
    WORKFLOW_FAILED = "WorkflowFailed"
    # V3 — timers and events (durable waits, ADR-0007/0021).
    TIMER_CREATED = "TimerCreated"
    TIMER_FIRED = "TimerFired"
    EVENT_WAIT_STARTED = "EventWaitStarted"
    EXTERNAL_EVENT_RECEIVED = "ExternalEventReceived"
    WORKFLOW_WAITING = "WorkflowWaiting"
    # V4 — composite primitives. A parent records this when it schedules a linked child
    # run (``satay.start_child``); the child's ``WorkflowCreated`` carries the reverse
    # ``parent_run_id`` + originating call identity, so the tree is recoverable both ways.
    CHILD_WORKFLOW_SCHEDULED = "ChildWorkflowScheduled"
    # V5 — control API. The worker appends this (via a queued ``cancel`` command, or a
    # direct ``RunHandle.cancel()``) to durably record a cancellation and halt the run.
    WORKFLOW_CANCELLED = "WorkflowCancelled"
    # Collect-mode fan-out (ADR-0027). The **terminal** failure of one logical task:
    # appended when a task exhausts its retries inside a ``return_exceptions=True``
    # composite, where the run survives the failure. It is the failure-side twin of
    # ``TaskCompleted`` — a recorded ``TaskFailed`` is a replay *hit* that re-raises
    # instead of re-executing. Fail-fast composites never append it (the terminal event
    # there is ``WorkflowFailed``), so existing journals are unchanged.
    TASK_FAILED = "TaskFailed"
    # V7 — fork. The worker appends this to a newly-forked run, right after seeding its
    # journal from the source's prefix, to record lineage: the ``source_run_id`` it was
    # branched from and the ``fork_point_seq`` (the last source event copied). A
    # fork-of-a-fork carries the ancestor's copied ``RunForked`` in its prefix; the
    # run's *own* fork record is the ``RunForked`` with the greatest ``seq`` (ADR-0004).
    RUN_FORKED = "RunForked"


class RunStatus(StrEnum):
    """Lifecycle status of a run, derived from its journal head."""

    RUNNING = "running"
    #: Durably parked on a ``sleep`` / ``wait_for_event`` — released from memory, no
    #: live frame (V3, ADR-0007). Non-terminal: the poll loop wakes it. A graceful wake
    #: writes no ``WorkflowResumed`` and carries no ⚡ (ADR-0009/Q52).
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    #: Terminally cancelled via the control API (V5). The worker appends
    #: ``WorkflowCancelled`` and stops driving the run; a cancelled run is never
    #: re-driven by the poll loop nor woken by a later timer/event.
    CANCELLED = "cancelled"


TERMINAL_STATUSES = frozenset({RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED})


class CallStatus(StrEnum):
    """Status of one durable call, as the read layer reports it.

    Shared by three sites that used to carry the same three bare strings with no name
    tying them together (ADR-0033's Consequences, ADR-0038): ``RecordedCall.status``
    (:mod:`satay.api.inspection`, :mod:`satay.api.diffing`), the per-*attempt* status
    inside the HTTP read API's ``task_detail`` view, and the map/tree per-item status
    rollup in :mod:`satay.control.views`. A task call, a fan-out item, and an attempt are
    always one of the first three values.

    ``WAITING`` and ``CANCELLED`` are reachable only for a ``start_child`` call, whose
    status mirrors its own child run's :class:`RunStatus` rather than a task's narrower
    three-value vocabulary. ``UNKNOWN`` is the defensive fallback for a child run record
    that cannot be found — should not happen given ADR-0004's no-deletion guarantee, but
    reported rather than raised, the same stance :func:`satay.inspect` takes elsewhere.

    The control plane's own ``cancelling``/``accepted`` (the third vocabulary ADR-0033
    named) is a *different* concept — an HTTP write endpoint's synchronous
    acknowledgement, not a call's or a run's status — and is not folded in here; see
    ``satay.control.server``'s local acknowledgement enum instead.
    """

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    WAITING = "waiting"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


def new_event_id() -> str:
    """Allocate a globally-unique event id."""
    return uuid.uuid4().hex


def utc_now() -> datetime:
    """Current timezone-aware UTC time (used only when no clock is injected)."""
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class Event:
    """The journal envelope (A3.1, ADR-0004).

    Total order per run is by ``seq`` (1-based, per-run monotonic, allocated inside
    the append transaction). ``seq == 0`` marks an event not yet appended.
    """

    run_id: str
    type: EventType
    payload: Mapping[str, Any] = field(default_factory=dict)
    ts: datetime = field(default_factory=utc_now)
    event_id: str = field(default_factory=new_event_id)
    seq: int = 0

    def with_seq(self, seq: int) -> Event:
        """Return a copy of this event stamped with its allocated ``seq``."""
        return Event(
            run_id=self.run_id,
            type=self.type,
            payload=self.payload,
            ts=self.ts,
            event_id=self.event_id,
            seq=seq,
        )


@dataclass(frozen=True, slots=True)
class RunRecord:
    """The ``runs``-table row: run identity and denormalised status."""

    run_id: str
    workflow_name: str
    status: RunStatus
    code_version: str
    created_at: datetime
    idempotency_key: str | None = None


class TimerKind(StrEnum):
    """What a timer row resolves when it fires (V3, ADR-0007)."""

    SLEEP = "sleep"
    EVENT_TIMEOUT = "event_timeout"


class TimerStatus(StrEnum):
    """Lifecycle of a timer row; the firing-idempotency guard keys on it (V3)."""

    PENDING = "pending"
    FIRED = "fired"
    #: The wait resolved by a delivered event before the timeout fired, so its timeout
    #: timer is dropped (ADR-0021 event-wins).
    DISCARDED = "discarded"


@dataclass(frozen=True, slots=True)
class TimerRecord:
    """A ``timers``-table row: a due-time the worker polls (V3, ADR-0007).

    ``identity`` is the durable-call identity string the timer resolves (a ``sleep``
    call site, or the ``wait_for_event`` whose timeout this bounds), tying the row to
    the ``TimerCreated`` / ``TimerFired`` journal events for replay.
    """

    timer_id: str
    run_id: str
    kind: TimerKind
    identity: str
    fire_at: datetime
    status: TimerStatus
    created_at: datetime


@dataclass(frozen=True, slots=True)
class InboxEventRecord:
    """An ``event_inbox``-table row: an external event awaiting a matching wait (V3).

    ``run_id`` is optional (``None`` = deliverable to any run); matching is by
    ``(event_type, key)`` (V3 design rule 3). ``payload_ref`` is the codec-encoded
    event structure (the same indirection as ``input_ref`` / ``output_ref``). Buffered
    matches are consumed FIFO by ``(received_at, row_id)`` (ADR-0021). ``row_id`` is
    assigned by the store on insert (``0`` before insertion).
    """

    event_type: str
    key: str | None
    payload_ref: Any
    received_at: datetime
    run_id: str | None = None
    consumed: bool = False
    row_id: int = 0
