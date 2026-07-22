"""Journal, codec, and persistence (A3).

The append-only event log and its serialization. Per ADR-0016 events are modeled as
**stdlib frozen dataclasses** and persisted via **raw parameterized SQL over stdlib
``sqlite3``** (no ORM); the codec is stdlib ``json`` with tagged types and **Pydantic
is duck-typed, not a core dependency** (ADR-0013). The ``Store`` seam isolates SQLite
today from PostgreSQL later (ARCHITECTURE §9).

This is scaffold: the ``Event`` envelope and ``Store`` seam are declared to fix the
shape; the event subset, codec, and ``SQLiteStore`` land in V1.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class Event:
    """The journal envelope (A3.1, ADR-0004). Payload is per-type (lands in V1).

    Total order per run is by ``seq``, which is the replay and timeline ordering key.
    """

    run_id: str
    seq: int
    event_id: str
    type: str
    ts: datetime
    payload: dict[str, Any]


class Store(Protocol):
    """Durable-store seam (ARCHITECTURE §9). Concrete ``SQLiteStore`` lands in V1.

    The worker is the sole writer; ``seq`` is allocated inside the append
    transaction on the writer thread (ADR-0012).
    """

    def append(self, event: Event) -> None:
        """Atomically append one event (one event, one transaction)."""
        ...

    def read_events(self, run_id: str) -> Sequence[Event]:
        """Read a run's events in ``seq`` order."""
        ...

    def list_runs(self) -> Iterable[str]:
        """List known run ids."""
        ...
