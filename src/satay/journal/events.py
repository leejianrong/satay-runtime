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
    """The journal event types active through V2 (a prefix of ADR-0004's full set)."""

    WORKFLOW_CREATED = "WorkflowCreated"
    WORKFLOW_RESUMED = "WorkflowResumed"
    TASK_SCHEDULED = "TaskScheduled"
    TASK_ATTEMPT_STARTED = "TaskAttemptStarted"
    TASK_ATTEMPT_FAILED = "TaskAttemptFailed"
    TASK_COMPLETED = "TaskCompleted"
    WORKFLOW_COMPLETED = "WorkflowCompleted"
    WORKFLOW_FAILED = "WorkflowFailed"


class RunStatus(StrEnum):
    """Lifecycle status of a run, derived from its journal head."""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


TERMINAL_STATUSES = frozenset({RunStatus.COMPLETED, RunStatus.FAILED})


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
