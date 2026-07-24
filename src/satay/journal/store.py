"""``SQLiteStore`` — the V1 durable store (A3.5, ADR-0017).

Raw parameterized SQL over the stdlib ``sqlite3`` driver — no ORM (ADR-0016). One
process, one writer (ADR-0007/0012): a single long-lived connection guarded by a
per-run :class:`asyncio.Lock`. Each event is one transaction, and its per-run ``seq``
is allocated (``MAX(seq)+1``) *inside* that transaction, which the single-writer model
makes sufficient without row locking.

Schema is versioned with ``PRAGMA user_version`` and migrated forward on open. A
temp-file path or ``":memory:"`` is supported (the latter keeps the one connection
alive for the store's lifetime, since a fresh connection is a fresh in-memory DB).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from satay.journal.codec import from_json, to_json
from satay.journal.events import (
    Event,
    EventType,
    InboxEventRecord,
    RunRecord,
    RunStatus,
    TimerKind,
    TimerRecord,
    TimerStatus,
)

#: The schema version this build writes. Forward-only migrations bring older DBs up.
#: v2 (V2 slice) adds the ``runs.idempotency_key`` index backing keyed ``satay.start``.
#: v3 (V3 slice) adds the ``timers`` table and the ``event_inbox`` table.
SCHEMA_VERSION = 3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id          TEXT PRIMARY KEY,
    workflow_name   TEXT NOT NULL,
    status          TEXT NOT NULL,
    code_version    TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    idempotency_key TEXT
);

CREATE TABLE IF NOT EXISTS events (
    run_id       TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    event_id     TEXT NOT NULL UNIQUE,
    type         TEXT NOT NULL,
    ts           TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
);
"""

#: The index backing keyed idempotent ``satay.start`` look-up (V2, build step 5).
_IDEMPOTENCY_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_runs_idempotency_key "
    "ON runs(idempotency_key) WHERE idempotency_key IS NOT NULL"
)

#: The V3 timer + event-inbox schema (ADR-0007 poll model, ADR-0021 ordering). The
#: inbox table is named ``event_inbox`` because ``events`` is already the journal table.
_SCHEMA_V3 = """
CREATE TABLE IF NOT EXISTS timers (
    timer_id     TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL,
    kind         TEXT NOT NULL,
    identity     TEXT NOT NULL,
    fire_at      TEXT NOT NULL,
    status       TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_timers_due ON timers(status, fire_at);

CREATE TABLE IF NOT EXISTS event_inbox (
    row_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT,
    event_type   TEXT NOT NULL,
    key          TEXT,
    payload_ref  TEXT NOT NULL,
    received_at  TEXT NOT NULL,
    consumed     INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_inbox_match
    ON event_inbox(event_type, key, consumed, received_at, row_id);
"""


class SQLiteStore:
    """Append-only journal store backed by stdlib ``sqlite3``.

    Construct with :meth:`open`; close with :meth:`close`. Safe as an async context
    manager.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._conn = connection
        self._run_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    @classmethod
    def open(cls, path: str | Path) -> SQLiteStore:
        """Open (creating if needed) a store at ``path`` (a file path or ``":memory:"``)."""
        conn = sqlite3.connect(
            str(path),
            isolation_level=None,  # explicit transaction control
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        store = cls(conn)
        store._migrate()
        return store

    def close(self) -> None:
        """Close the underlying connection."""
        self._conn.close()

    async def __aenter__(self) -> SQLiteStore:
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.close()

    # -- schema ------------------------------------------------------------------

    def _migrate(self) -> None:
        current = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if current > SCHEMA_VERSION:
            raise RuntimeError(
                f"database written by a newer satay (user_version={current} > "
                f"{SCHEMA_VERSION}); refusing to open (ADR-0017)"
            )
        if current < 1:
            self._conn.executescript(_SCHEMA)
        if current < 2:
            # v1 → v2: add the idempotency-key index (idempotent under IF NOT EXISTS).
            self._conn.execute(_IDEMPOTENCY_INDEX)
        if current < 3:
            # v2 → v3: add the timers + event-inbox tables (idempotent under IF NOT EXISTS).
            self._conn.executescript(_SCHEMA_V3)
        if current < SCHEMA_VERSION:
            self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    # -- writes ------------------------------------------------------------------

    async def create_run(self, record: RunRecord) -> None:
        """Insert a new run row; a repeated ``run_id`` is ignored (idempotent)."""
        async with self._run_locks[record.run_id]:
            self._conn.execute(
                "INSERT OR IGNORE INTO runs "
                "(run_id, workflow_name, status, code_version, created_at, idempotency_key) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    record.run_id,
                    record.workflow_name,
                    record.status.value,
                    record.code_version,
                    record.created_at.isoformat(),
                    record.idempotency_key,
                ),
            )

    async def append(self, event: Event) -> Event:
        """Atomically append one event, allocating its per-run ``seq`` in-transaction."""
        async with self._run_locks[event.run_id]:
            conn = self._conn
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT COALESCE(MAX(seq), 0) FROM events WHERE run_id = ?",
                    (event.run_id,),
                ).fetchone()
                seq = int(row[0]) + 1
                conn.execute(
                    "INSERT INTO events (run_id, seq, event_id, type, ts, payload_json) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        event.run_id,
                        seq,
                        event.event_id,
                        event.type.value,
                        event.ts.isoformat(),
                        to_json(dict(event.payload)),
                    ),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            return event.with_seq(seq)

    async def set_status(self, run_id: str, status: RunStatus) -> None:
        """Update a run's denormalised status."""
        async with self._run_locks[run_id]:
            self._conn.execute(
                "UPDATE runs SET status = ? WHERE run_id = ?",
                (status.value, run_id),
            )

    # -- reads -------------------------------------------------------------------

    async def read_events(self, run_id: str) -> Sequence[Event]:
        """Read a run's events in ``seq`` order."""
        rows = self._conn.execute(
            "SELECT run_id, seq, event_id, type, ts, payload_json "
            "FROM events WHERE run_id = ? ORDER BY seq",
            (run_id,),
        ).fetchall()
        return [self._row_to_event(row) for row in rows]

    async def get_run(self, run_id: str) -> RunRecord | None:
        """Return the run record, or ``None`` if unknown."""
        row = self._conn.execute(
            "SELECT run_id, workflow_name, status, code_version, created_at, idempotency_key "
            "FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        return RunRecord(
            run_id=row["run_id"],
            workflow_name=row["workflow_name"],
            status=RunStatus(row["status"]),
            code_version=row["code_version"],
            created_at=datetime.fromisoformat(row["created_at"]),
            idempotency_key=row["idempotency_key"],
        )

    async def list_runs(self) -> Sequence[str]:
        """List known run ids, oldest first."""
        rows = self._conn.execute("SELECT run_id FROM runs ORDER BY created_at").fetchall()
        return [row["run_id"] for row in rows]

    async def get_run_by_idempotency_key(self, idempotency_key: str) -> RunRecord | None:
        """Return the earliest run created with ``idempotency_key`` (keyed start, N13)."""
        row = self._conn.execute(
            "SELECT run_id, workflow_name, status, code_version, created_at, idempotency_key "
            "FROM runs WHERE idempotency_key = ? ORDER BY created_at LIMIT 1",
            (idempotency_key,),
        ).fetchone()
        if row is None:
            return None
        return RunRecord(
            run_id=row["run_id"],
            workflow_name=row["workflow_name"],
            status=RunStatus(row["status"]),
            code_version=row["code_version"],
            created_at=datetime.fromisoformat(row["created_at"]),
            idempotency_key=row["idempotency_key"],
        )

    # -- timers (V3, ADR-0007) ---------------------------------------------------

    async def add_timer(self, timer: TimerRecord) -> None:
        """Persist a timer row; a repeated ``timer_id`` is ignored (idempotent)."""
        async with self._run_locks[timer.run_id]:
            self._conn.execute(
                "INSERT OR IGNORE INTO timers "
                "(timer_id, run_id, kind, identity, fire_at, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    timer.timer_id,
                    timer.run_id,
                    timer.kind.value,
                    timer.identity,
                    timer.fire_at.isoformat(),
                    timer.status.value,
                    timer.created_at.isoformat(),
                ),
            )

    async def due_timers(self, now: datetime) -> Sequence[TimerRecord]:
        """Return ``pending`` timers with ``fire_at <= now``, earliest ``fire_at`` first."""
        rows = self._conn.execute(
            "SELECT timer_id, run_id, kind, identity, fire_at, status, created_at "
            "FROM timers WHERE status = ? AND fire_at <= ? ORDER BY fire_at, timer_id",
            (TimerStatus.PENDING.value, now.isoformat()),
        ).fetchall()
        return [self._row_to_timer(row) for row in rows]

    async def set_timer_status(self, timer_id: str, status: TimerStatus) -> None:
        """Update a timer's status (the firing-idempotency guard)."""
        self._conn.execute(
            "UPDATE timers SET status = ? WHERE timer_id = ?",
            (status.value, timer_id),
        )

    # -- event inbox (V3, ADR-0021) ----------------------------------------------

    async def add_inbox_event(self, event: InboxEventRecord) -> InboxEventRecord:
        """Persist an inbox event; returns it stamped with its assigned ``row_id``."""
        conn = self._conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "INSERT INTO event_inbox "
                "(run_id, event_type, key, payload_ref, received_at, consumed) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    event.run_id,
                    event.event_type,
                    event.key,
                    json.dumps(event.payload_ref, separators=(",", ":")),
                    event.received_at.isoformat(),
                    1 if event.consumed else 0,
                ),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        row_id = int(cursor.lastrowid or 0)
        return InboxEventRecord(
            event_type=event.event_type,
            key=event.key,
            payload_ref=event.payload_ref,
            received_at=event.received_at,
            run_id=event.run_id,
            consumed=event.consumed,
            row_id=row_id,
        )

    async def match_inbox_event(self, event_type: str, key: str | None) -> InboxEventRecord | None:
        """Return the earliest unconsumed event matching ``(event_type, key)`` (FIFO)."""
        if key is None:
            row = self._conn.execute(
                "SELECT row_id, run_id, event_type, key, payload_ref, received_at, consumed "
                "FROM event_inbox WHERE event_type = ? AND key IS NULL AND consumed = 0 "
                "ORDER BY received_at, row_id LIMIT 1",
                (event_type,),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT row_id, run_id, event_type, key, payload_ref, received_at, consumed "
                "FROM event_inbox WHERE event_type = ? AND key = ? AND consumed = 0 "
                "ORDER BY received_at, row_id LIMIT 1",
                (event_type, key),
            ).fetchone()
        return None if row is None else self._row_to_inbox(row)

    async def consume_inbox_event(self, row_id: int) -> None:
        """Mark an inbox event consumed so it is never delivered twice."""
        self._conn.execute(
            "UPDATE event_inbox SET consumed = 1 WHERE row_id = ?",
            (row_id,),
        )

    async def list_inbox_events(
        self, *, event_type: str | None = None, include_consumed: bool = True
    ) -> Sequence[InboxEventRecord]:
        """List inbox events (for disposition assertions at run end, V3 design rule 3)."""
        clauses: list[str] = []
        params: list[object] = []
        if event_type is not None:
            clauses.append("event_type = ?")
            params.append(event_type)
        if not include_consumed:
            clauses.append("consumed = 0")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(
            "SELECT row_id, run_id, event_type, key, payload_ref, received_at, consumed "
            f"FROM event_inbox {where} ORDER BY received_at, row_id",
            tuple(params),
        ).fetchall()
        return [self._row_to_inbox(row) for row in rows]

    @staticmethod
    def _row_to_timer(row: sqlite3.Row) -> TimerRecord:
        return TimerRecord(
            timer_id=row["timer_id"],
            run_id=row["run_id"],
            kind=TimerKind(row["kind"]),
            identity=row["identity"],
            fire_at=datetime.fromisoformat(row["fire_at"]),
            status=TimerStatus(row["status"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    @staticmethod
    def _row_to_inbox(row: sqlite3.Row) -> InboxEventRecord:
        return InboxEventRecord(
            event_type=row["event_type"],
            key=row["key"],
            payload_ref=json.loads(row["payload_ref"]),
            received_at=datetime.fromisoformat(row["received_at"]),
            run_id=row["run_id"],
            consumed=bool(row["consumed"]),
            row_id=int(row["row_id"]),
        )

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> Event:
        return Event(
            run_id=row["run_id"],
            type=EventType(row["type"]),
            payload=from_json(row["payload_json"]),
            ts=datetime.fromisoformat(row["ts"]),
            event_id=row["event_id"],
            seq=int(row["seq"]),
        )
