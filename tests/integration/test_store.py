"""Integration tests for ``SQLiteStore`` seq allocation and isolation (N8, ADR-0012)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from satay.journal.events import Event, EventType, RunRecord, RunStatus
from satay.journal.store import SQLiteStore


def _run(run_id: str) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        workflow_name="demo",
        status=RunStatus.RUNNING,
        code_version="dev:test",
        created_at=datetime(2026, 7, 22, tzinfo=UTC),
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


async def test_refuses_database_from_newer_satay(tmp_path: object) -> None:
    from pathlib import Path

    assert isinstance(tmp_path, Path)
    db = tmp_path / "satay.db"
    store = SQLiteStore.open(db)
    store._conn.execute("PRAGMA user_version=999")
    store.close()
    with pytest.raises(RuntimeError, match="newer satay"):
        SQLiteStore.open(db)
