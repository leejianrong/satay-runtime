"""``SQLiteStore`` — the V1 durable store (A3.5, ADR-0017).

Raw parameterized SQL over the stdlib ``sqlite3`` driver — no ORM (ADR-0016). One
process, one writer (ADR-0007/0012): a single long-lived connection guarded by a
per-run :class:`asyncio.Lock`. Each event is one transaction, and its per-run ``seq``
is allocated (``MAX(seq)+1``) *inside* that transaction, which the single-writer model
makes sufficient without row locking.

Schema is versioned with ``PRAGMA user_version`` and migrated forward on open. A
temp-file path or ``":memory:"`` is supported (the latter keeps the one connection
alive for the store's lifetime, since a fresh connection is a fresh in-memory DB).

**Decoded events are memoised per run, for this store's own lifetime** (ADR-0036,
ARCHITECTURE §9): :meth:`read_events` fetches and decodes only what has been appended
since its last call for that run, rather than re-decoding the whole journal on every
drive. Safe because the journal is append-only for a *live* run (ADR-0004) and the
cache never outlives the process; :meth:`delete_run` (ADR-0039) evicts a deleted run's
cache entry, the one case an entry is ever removed rather than only extended.

**Write-time redaction (ADR-0029) is off by default.** Turned on — with
``write_redaction="on"`` or ``SATAY_WRITE_REDACTION=on`` — the recording path scrubs
sensitive values out of the ``*_ref`` value slots before they are serialized or spilled,
so the store never holds them and the redacted form is what a resumed run replays
against. Off, the store records verbatim and the redactor runs only on the read path,
which is the right shape for a local debugger (ADR-0009/0014).
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from satay.blobs import BlobStore, is_blob_ref, rehydrate_encoded, spill_encoded
from satay.config import BLOB_DIR_NAME, WriteRedaction, resolve_write_redaction
from satay.journal.codec import decode, encode
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
)
from satay.redaction import Redactor, is_value_slot

_LOG = logging.getLogger("satay")

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

    def __init__(
        self,
        connection: sqlite3.Connection,
        blobs: BlobStore | None = None,
        *,
        write_redactor: Redactor | None = None,
    ) -> None:
        self._conn = connection
        self._run_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        #: Decoded-event memoisation, within this store's own lifetime (ADR-0036,
        #: ARCHITECTURE §9). A tuple, not a list: immutable, so :meth:`read_events` can
        #: hand back the cached object itself on a cache hit rather than copying it.
        #: Journals are append-only (ADR-0004: no deletion, no compaction), so a cached
        #: prefix is only ever extended, never invalidated.
        self._event_cache: dict[str, tuple[Event, ...]] = {}
        #: Where over-threshold payloads spill (N19). ``None`` disables spill entirely,
        #: which is the case for a purely in-memory store; a file-backed store derives a
        #: sibling ``blobs/`` directory automatically so spill "just works" (ADR-0004).
        self._blobs = blobs
        #: Set when write-time redaction is on (ADR-0029): sensitive values are scrubbed
        #: on the recording path, so they never reach SQLite or a blob. ``None`` — the
        #: default — records verbatim and leaves redaction to the read path (ADR-0009).
        self._write_redactor = write_redactor

    @classmethod
    def open(
        cls,
        path: str | Path,
        *,
        blobs: BlobStore | None = None,
        write_redaction: str | WriteRedaction | None = None,
        redactor: Redactor | None = None,
    ) -> SQLiteStore:
        """Open (creating if needed) a store at ``path`` (a file path or ``":memory:"``).

        A file-backed store auto-attaches a :class:`~satay.blobs.BlobStore` in a sibling
        ``blobs/`` directory (so spilled payloads survive across processes and rehydrate
        transparently on read); ``":memory:"`` stays spill-free. Pass ``blobs`` to
        override.

        ``write_redaction`` selects the ADR-0029 recording-path mode, resolved explicit →
        ``SATAY_WRITE_REDACTION`` → :attr:`~satay.config.WriteRedaction.OFF`. With it on,
        the store scrubs sensitive values *before* they are serialized or spilled, so the
        redacted form is what the journal holds and what the run resumes against. Pass
        ``redactor`` to supply a non-default pattern set; it is used only when the mode
        is on.
        """
        conn = sqlite3.connect(
            str(path),
            isolation_level=None,  # explicit transaction control
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        if blobs is None and str(path) != ":memory:":
            blobs = BlobStore(Path(path).parent / BLOB_DIR_NAME)
        mode = resolve_write_redaction(write_redaction)
        write_redactor = (redactor or Redactor()) if mode.enabled else None
        store = cls(conn, blobs, write_redactor=write_redactor)
        store._migrate()
        return store

    @property
    def write_redaction_enabled(self) -> bool:
        """Whether this store redacts sensitive values on the write path (ADR-0029)."""
        return self._write_redactor is not None

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
                        self._encode_payload(event.payload, event.type, event.run_id),
                    ),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            return event.with_seq(seq)

    def _encode_payload(
        self,
        payload: object,
        event_type: EventType | None = None,
        run_id: str | None = None,
    ) -> str:
        """Encode an event payload to JSON, redacting then spilling as configured.

        Three steps, in this order:

        1. **Encode** to the JSON-compatible tagged form (ADR-0005).
        2. **Redact**, when write-time redaction is on (ADR-0029) — scoped to the
           ``*_ref`` value slots, so the structural fields replay reads (``task_name``,
           ``ordinal``, ``key``, ``identity``, ids) are handed through untouched.
        3. **Spill** over-threshold values to blobs (N19), replacing them with a
           reference so the journal never inlines a large payload (ADR-0004). Spill is
           disabled (always inline) when no blob store is attached.

        Redaction comes **before** spill deliberately: a redacted value must never reach
        a blob file either, and the placeholder is small enough that it usually removes
        the reason to spill at all.
        """
        encoded = encode(dict(payload) if isinstance(payload, Mapping) else payload)
        if self._write_redactor is not None:
            redacted = self._write_redactor.redact_value_slots(encoded)
            self._warn_if_resume_seed_redacted(encoded, redacted, event_type, run_id)
            encoded = redacted
        if self._blobs is not None:
            encoded = spill_encoded(encoded, self._blobs)
        return json.dumps(encoded, separators=(",", ":"))

    @staticmethod
    def _warn_if_resume_seed_redacted(
        before: Any,
        after: Any,
        event_type: EventType | None,
        run_id: str | None,
    ) -> None:
        """Warn when redaction rewrites a ``WorkflowCreated`` input — the resume seed.

        Every other redacted slot is a *record* of something that already happened. A
        workflow's ``input_ref`` is different: it is the value the run is re-entered with
        on resume and on fork (:mod:`satay.timers`, :mod:`satay.control.commands`), so
        redacting it changes what the workflow body computes from past the replay
        frontier. That is the intended semantics of the mode — the redacted form is what
        the run resumes against (ADR-0026/0029) — but it is worth saying out loud once
        per run, because the fix is to fetch the secret inside a task rather than pass it
        as workflow input.
        """
        if event_type is not EventType.WORKFLOW_CREATED:
            return
        if not isinstance(before, Mapping) or not isinstance(after, Mapping):
            return
        if before.get("input_ref") == after.get("input_ref"):
            return
        _LOG.warning(
            "write_redaction: redacted the workflow input of run %s; the run will resume "
            "and fork from the redacted value, not the original (ADR-0029). Pass secrets "
            "to a task, or fetch them inside one, rather than as workflow input.",
            run_id,
        )

    async def set_status(self, run_id: str, status: RunStatus) -> None:
        """Update a run's denormalised status."""
        async with self._run_locks[run_id]:
            self._conn.execute(
                "UPDATE runs SET status = ? WHERE run_id = ?",
                (status.value, run_id),
            )

    # -- reads -------------------------------------------------------------------

    async def read_events(self, run_id: str) -> Sequence[Event]:
        """Read a run's events in ``seq`` order.

        Memoised per run for this store's lifetime (ADR-0036, ARCHITECTURE §9): a
        repeat call — the common case, since every drive of a run re-reads its whole
        journal from the top (ADR-0001) — fetches and decodes only the rows appended
        since the last call, then extends the cached tuple, instead of re-decoding a
        journal this process has already decoded once. A run this store has never read
        before still pays the full cost of its journal so far, once.

        Safe under the append-only journal (ADR-0004: no compaction, no rewriting a
        live run's history): a cached prefix is a fact that stays true forever, so
        there is nothing to invalidate, only more to append — except a run that
        :meth:`delete_run` (ADR-0039) has removed outright, which evicts this cache
        too. Scoped to this store's own lifetime, same as every other in-memory state
        here — a fresh :meth:`open` starts with an empty cache, so this never persists
        beyond the process (ADR-0012 single-writer, one store per process).
        """
        async with self._run_locks[run_id]:
            cached = self._event_cache.get(run_id, ())
            after_seq = cached[-1].seq if cached else 0
            rows = self._conn.execute(
                "SELECT run_id, seq, event_id, type, ts, payload_json "
                "FROM events WHERE run_id = ? AND seq > ? ORDER BY seq",
                (run_id, after_seq),
            ).fetchall()
            if not rows:
                return cached
            merged = cached + tuple(self._row_to_event(row) for row in rows)
            self._event_cache[run_id] = merged
            return merged

    def _decode_payload(self, payload_json: str) -> Any:
        """Decode a stored payload, rehydrating any spilled blob references first (N19).

        Blob references are resolved back to their inline encoded form *before* the codec
        decodes, so a spilled payload yields exactly the value an inline one would — the
        rehydration is invisible to every reader above the store (replay, read API,
        Studio), and redaction (which runs later, on the read view) therefore scrubs a
        spilled field identically to an inline one (ADR-0004/ADR-0014).

        ``decode`` collapses a tagged dataclass/model to a
        :class:`~satay.journal.codec.TaggedDict` — a plain mapping of its fields that
        keeps the encoder's ``"type"`` discriminator as an *attribute*. Readers that just
        walk the payload see the same dict they always did; ``rehydrate`` on the replay
        path gets the exact signal it needs to pick a union arm (KAN-520, ADR-0031).
        """
        raw = json.loads(payload_json)
        raw = rehydrate_encoded(raw, self._blobs)
        return decode(raw)

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

    async def delete_run(self, run_id: str) -> None:
        """Delete one run's ``runs`` and ``events`` rows outright (ADR-0037/0039).

        The run must be terminal (:data:`~satay.journal.events.TERMINAL_STATUSES` —
        completed, failed, or cancelled), the same precondition ``fork`` already uses,
        so a driver that might still resume a run can never have it deleted from under
        it. Raises ``LookupError`` for an unknown run, ``ValueError`` for a non-terminal
        one.

        Touches only this run's own rows: no cascade to a fork's lineage or a parent's
        `child_run_id`, and no blob deletion — a fork or an unrelated run can still
        name the exact same content-addressed blob (ADR-0004/Q54), so blob GC is a
        separate, reference-aware sweep (:mod:`satay.blobs.gc`) run independently of
        any single deletion. A dangling `child_run_id` / `source_run_id` after this is
        the same "unknown run id" case `inspect`/`diff` already tolerate.
        """
        async with self._run_locks[run_id]:
            record = await self.get_run(run_id)
            if record is None:
                raise LookupError(f"run {run_id!r} not found")
            if record.status not in TERMINAL_STATUSES:
                allowed = ", ".join(sorted(s.value for s in TERMINAL_STATUSES))
                raise ValueError(
                    f"cannot delete run {run_id!r}: status is {record.status.value!r}, "
                    f"not terminal ({allowed})"
                )
            conn = self._conn
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("DELETE FROM events WHERE run_id = ?", (run_id,))
                conn.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            self._event_cache.pop(run_id, None)

    async def referenced_blob_ids(self) -> set[str]:
        """Every blob id still named by any remaining run's journal (ADR-0037/0039).

        The **mark** half of blob GC's mark-and-sweep. Reads ``payload_json`` straight
        from SQLite — *before* :meth:`_decode_payload`'s blob-reference rehydration —
        because rehydration resolves a spilled reference into its full inline value,
        which would hide every blob reference from a mark phase built on
        :meth:`read_events` instead. Value slots are selected the same way write-time
        redaction selects them (:func:`satay.redaction.is_value_slot`, suffix-based so
        a future ``*_ref`` field is covered automatically); a slot's raw value counts
        only when :func:`satay.blobs.is_blob_ref` recognises its shape. Recomputed from
        scratch on every call — no incremental index to drift (the same reasoning
        ADR-0036's cache uses, applied here to correctness instead of performance).

        Deliberately does not scan ``event_inbox``: its own ``payload_ref`` column is
        redacted on write but never spilled (confirmed by reading
        :meth:`add_inbox_event`, which calls no ``spill_encoded``), so a blob
        reference cannot originate there.
        """
        ids: set[str] = set()
        rows = self._conn.execute("SELECT payload_json FROM events").fetchall()
        for row in rows:
            payload = json.loads(row["payload_json"])
            if not isinstance(payload, Mapping):
                continue
            for key, value in payload.items():
                if is_value_slot(key) and is_blob_ref(value):
                    ids.add(str(value["id"]))
        return ids

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
        """Persist an inbox event; returns it stamped with its assigned ``row_id``.

        ``payload_ref`` is a value slot in its own column — the same indirection the
        journal's ``event_ref`` uses — so write-time redaction scrubs it here too
        (ADR-0029). The waiting workflow is then delivered the redacted form, and the
        ``ExternalEventReceived`` the engine copies it into is already clean. Matching
        keys on ``(event_type, key)``, both structural and both untouched.
        """
        payload_ref = event.payload_ref
        if self._write_redactor is not None:
            payload_ref = self._write_redactor.redact(payload_ref)
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
                    json.dumps(payload_ref, separators=(",", ":")),
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
            payload_ref=payload_ref,
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

    def _row_to_event(self, row: sqlite3.Row) -> Event:
        return Event(
            run_id=row["run_id"],
            type=EventType(row["type"]),
            payload=self._decode_payload(row["payload_json"]),
            ts=datetime.fromisoformat(row["ts"]),
            event_id=row["event_id"],
            seq=int(row["seq"]),
        )
