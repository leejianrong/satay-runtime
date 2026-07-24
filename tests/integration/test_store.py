"""Integration tests for ``SQLiteStore`` seq allocation and isolation (N8, ADR-0012)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from satay.journal.events import Event, EventType, RunRecord, RunStatus
from satay.journal.store import SQLiteStore


def _run(run_id: str, *, idempotency_key: str | None = None) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        workflow_name="demo",
        status=RunStatus.RUNNING,
        code_version="dev:test",
        created_at=datetime(2026, 7, 22, tzinfo=UTC),
        idempotency_key=idempotency_key,
    )


def _event(run_id: str, etype: EventType = EventType.TASK_SCHEDULED) -> Event:
    return Event(run_id=run_id, type=etype, payload={"n": 1})


async def test_append_allocates_monotonic_seq() -> None:
    store = SQLiteStore.open(":memory:")
    await store.create_run(_run("r1"))
    seqs = [(await store.append(_event("r1"))).seq for _ in range(4)]
    assert seqs == [1, 2, 3, 4]
    store.close()


async def test_seq_is_isolated_per_run() -> None:
    store = SQLiteStore.open(":memory:")
    await store.create_run(_run("r1"))
    await store.create_run(_run("r2"))
    # Interleave appends across two runs; each run keeps its own 1-based sequence.
    a1 = await store.append(_event("r1"))
    b1 = await store.append(_event("r2"))
    a2 = await store.append(_event("r1"))
    b2 = await store.append(_event("r2"))
    assert (a1.seq, a2.seq) == (1, 2)
    assert (b1.seq, b2.seq) == (1, 2)
    store.close()


async def test_read_events_ordered_by_seq() -> None:
    store = SQLiteStore.open(":memory:")
    await store.create_run(_run("r1"))
    for _ in range(3):
        await store.append(_event("r1"))
    events = await store.read_events("r1")
    assert [e.seq for e in events] == [1, 2, 3]
    store.close()


async def test_create_run_is_idempotent() -> None:
    store = SQLiteStore.open(":memory:")
    await store.create_run(_run("r1"))
    await store.create_run(_run("r1"))  # ignored, no error
    assert await store.list_runs() == ["r1"]
    store.close()


async def test_set_status_and_get_run() -> None:
    store = SQLiteStore.open(":memory:")
    await store.create_run(_run("r1"))
    await store.set_status("r1", RunStatus.COMPLETED)
    record = await store.get_run("r1")
    assert record is not None
    assert record.status is RunStatus.COMPLETED
    assert await store.get_run("missing") is None
    store.close()


async def test_get_run_by_idempotency_key_uses_the_index() -> None:
    store = SQLiteStore.open(":memory:")
    await store.create_run(_run("r1", idempotency_key="order-42"))
    await store.create_run(_run("r2", idempotency_key="order-99"))
    await store.create_run(_run("r3"))  # no key

    found = await store.get_run_by_idempotency_key("order-42")
    assert found is not None
    assert found.run_id == "r1"
    assert await store.get_run_by_idempotency_key("missing") is None
    store.close()


async def test_v1_database_migrates_forward(tmp_path: object) -> None:
    """A v1 database opens and gains the idempotency index plus the V3 tables."""
    from pathlib import Path

    assert isinstance(tmp_path, Path)
    db = tmp_path / "satay.db"
    store = SQLiteStore.open(db)
    store._conn.execute("PRAGMA user_version=1")  # pretend it was written by v1
    store.close()

    reopened = SQLiteStore.open(db)  # migrates 1 → 3
    assert reopened._conn.execute("PRAGMA user_version").fetchone()[0] == 3
    names = {
        row[0]
        for row in reopened._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }
    assert "idx_runs_idempotency_key" in names
    reopened.close()


async def test_v2_database_migrates_to_v3_tables(tmp_path: object) -> None:
    """A v2 database opens and gains the ``timers`` and ``event_inbox`` tables (V3)."""
    from pathlib import Path

    assert isinstance(tmp_path, Path)
    db = tmp_path / "satay.db"
    store = SQLiteStore.open(db)
    store._conn.execute("PRAGMA user_version=2")  # pretend it was written by v2
    store.close()

    reopened = SQLiteStore.open(db)  # migrates 2 → 3
    assert reopened._conn.execute("PRAGMA user_version").fetchone()[0] == 3
    tables = {
        row[0]
        for row in reopened._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert {"timers", "event_inbox"} <= tables
    reopened.close()


async def test_refuses_database_from_newer_satay(tmp_path: object) -> None:
    from pathlib import Path

    assert isinstance(tmp_path, Path)
    db = tmp_path / "satay.db"
    store = SQLiteStore.open(db)
    store._conn.execute("PRAGMA user_version=999")
    store.close()
    with pytest.raises(RuntimeError, match="newer satay"):
        SQLiteStore.open(db)
