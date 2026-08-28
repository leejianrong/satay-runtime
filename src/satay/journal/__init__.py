"""Journal, codec, and persistence (A3).

The append-only event log and its serialization. Per ADR-0016 events are modeled as
**stdlib frozen dataclasses** (see :mod:`satay.journal.events`) and persisted via
**raw parameterized SQL over stdlib ``sqlite3``** (no ORM, see
:mod:`satay.journal.store`); the codec is stdlib ``json`` with tagged types and
**Pydantic is duck-typed, not a core dependency** (ADR-0013, see
:mod:`satay.journal.codec`). The ``Store`` seam isolates SQLite today from PostgreSQL
later (ARCHITECTURE §9).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from satay.journal.events import (
    TERMINAL_STATUSES,
    Event,
    EventType,
    InboxEventRecord,
    RunRecord,
    RunStatus,
    TimerKind,
    TimerRecord,
    TimerStatus,
    new_event_id,
    utc_now,
)


class Store(Protocol):
    """Durable-store seam (ARCHITECTURE §9); :class:`satay.journal.store.SQLiteStore` is
    the only implementation today, and the PostgreSQL backend the seam anticipates is
    out of MVP scope.

    The worker is the sole writer; ``seq`` is allocated inside the append
    transaction under a per-run async writer lock (ADR-0012). One event is one
    transaction, and ``TaskCompleted``'s commit is the durability point the
    crash-recovery proof hinges on (ADR-0004).
    """

    async def create_run(self, record: RunRecord) -> None:
        """Insert a new run row (idempotent on ``run_id``)."""
        ...

    async def append(self, event: Event) -> Event:
        """Atomically append one event, allocating its per-run ``seq``.

        Returns the event stamped with the allocated ``seq``.
        """
        ...

    async def read_events(self, run_id: str) -> Sequence[Event]:
        """Read a run's events in ``seq`` order."""
        ...

    async def get_run(self, run_id: str) -> RunRecord | None:
        """Return the run record, or ``None`` if the run is unknown."""
        ...

    async def get_run_by_idempotency_key(self, idempotency_key: str) -> RunRecord | None:
        """Return the run created with ``idempotency_key`` (keyed start, N13), or ``None``."""
        ...

    async def set_status(self, run_id: str, status: RunStatus) -> None:
        """Update a run's denormalised status."""
        ...

    async def list_runs(self) -> Sequence[str]:
        """List known run ids."""
        ...

    async def delete_run(self, run_id: str) -> None:
        """Delete one run's rows outright (ADR-0037/0039). Terminal runs only."""
        ...

    async def referenced_blob_ids(self) -> set[str]:
        """Every blob id still named by any remaining run's journal (ADR-0037/0039)."""
        ...

    # -- timers and events (V3, ADR-0007/0021) -----------------------------------

    async def add_timer(self, timer: TimerRecord) -> None:
        """Persist a timer row (idempotent on ``timer_id``)."""
        ...

    async def due_timers(self, now: datetime) -> Sequence[TimerRecord]:
        """Return ``pending`` timers with ``fire_at <= now``, earliest ``fire_at`` first."""
        ...

    async def set_timer_status(self, timer_id: str, status: TimerStatus) -> None:
        """Update a timer's status (the firing-idempotency guard)."""
        ...

    async def add_inbox_event(self, event: InboxEventRecord) -> InboxEventRecord:
        """Persist an inbox event; returns it stamped with its assigned ``row_id``."""
        ...

    async def match_inbox_event(self, event_type: str, key: str | None) -> InboxEventRecord | None:
        """Return the earliest unconsumed event matching ``(event_type, key)`` (FIFO)."""
        ...

    async def consume_inbox_event(self, row_id: int) -> None:
        """Mark an inbox event consumed so it is never delivered twice."""
        ...

    async def list_inbox_events(
        self, *, event_type: str | None = None, include_consumed: bool = True
    ) -> Sequence[InboxEventRecord]:
        """List inbox events (for disposition assertions at run end, V3 design rule 3)."""
        ...


__all__ = [
    "TERMINAL_STATUSES",
    "Event",
    "EventType",
    "InboxEventRecord",
    "RunRecord",
    "RunStatus",
    "Store",
    "TimerKind",
    "TimerRecord",
    "TimerStatus",
    "new_event_id",
    "utc_now",
]
