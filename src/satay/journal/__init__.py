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
from typing import Protocol

from satay.journal.events import (
    TERMINAL_STATUSES,
    Event,
    EventType,
    RunRecord,
    RunStatus,
    new_event_id,
    utc_now,
)


class Store(Protocol):
    """Durable-store seam (ARCHITECTURE §9). Concrete ``SQLiteStore`` lands in V1.

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

    async def set_status(self, run_id: str, status: RunStatus) -> None:
        """Update a run's denormalised status."""
        ...

    async def list_runs(self) -> Sequence[str]:
        """List known run ids."""
        ...


__all__ = [
    "TERMINAL_STATUSES",
    "Event",
    "EventType",
    "RunRecord",
    "RunStatus",
    "Store",
    "new_event_id",
    "utc_now",
]
