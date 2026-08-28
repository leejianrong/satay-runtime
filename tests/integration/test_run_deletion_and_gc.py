"""Integration tests for run deletion and blob GC mark-and-sweep (ADR-0037, ADR-0039).

Two layers: the store's ``delete_run``/``referenced_blob_ids`` (the delete primitive and
the mark phase), and :mod:`satay.blobs.gc` (the sweep). A fork-sharing test proves the
reference-aware property the whole design exists for (ADR-0004/Q54): a blob named by two
runs' journals survives the deletion of either one alone.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from satay.blobs import BlobStore, make_blob_ref
from satay.blobs.gc import collect_garbage
from satay.config import blob_dir, db_path
from satay.control.commands import create_fork
from satay.journal.events import Event, EventType, RunRecord, RunStatus
from satay.journal.store import SQLiteStore


def _run(run_id: str, status: RunStatus = RunStatus.RUNNING) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        workflow_name="demo",
        status=status,
        code_version="dev:test",
        created_at=datetime(2026, 7, 22, tzinfo=UTC),
    )


async def _with_spilled_output(
    store: SQLiteStore, blobs: BlobStore, run_id: str, value: str
) -> str:
    """Create a terminal run whose ``TaskCompleted.output_ref`` names a real blob.

    ``blobs.put`` stores whatever bytes it is given, but ``rehydrate_encoded`` always
    ``json.loads``s them back (a real spill only ever writes the JSON-encoded form of a
    value) — so the blob content here must be valid JSON, not arbitrary bytes.
    """
    data = json.dumps(value).encode()
    blob_id = blobs.put(data)
    await store.create_run(_run(run_id, RunStatus.RUNNING))
    await store.append(
        Event(
            run_id=run_id,
            type=EventType.TASK_COMPLETED,
            payload={
                "task_name": "t",
                "ordinal": 0,
                "output_ref": make_blob_ref(blob_id, len(data)),
            },
        )
    )
    await store.set_status(run_id, RunStatus.COMPLETED)
    return blob_id


# -- delete_run ----------------------------------------------------------------------


async def test_delete_run_removes_runs_and_events_rows() -> None:
    store = SQLiteStore.open(":memory:")
    await store.create_run(_run("r1", RunStatus.COMPLETED))
    await store.append(Event(run_id="r1", type=EventType.WORKFLOW_COMPLETED, payload={}))

    await store.delete_run("r1")

    assert await store.get_run("r1") is None
    assert await store.read_events("r1") == ()
    store.close()


async def test_delete_run_is_isolated_to_one_run() -> None:
    store = SQLiteStore.open(":memory:")
    await store.create_run(_run("r1", RunStatus.COMPLETED))
    await store.create_run(_run("r2", RunStatus.COMPLETED))
    await store.append(Event(run_id="r2", type=EventType.WORKFLOW_COMPLETED, payload={}))

    await store.delete_run("r1")

    assert await store.get_run("r2") is not None
    assert len(await store.read_events("r2")) == 1
    store.close()


async def test_delete_run_rejects_a_non_terminal_run() -> None:
    store = SQLiteStore.open(":memory:")
    await store.create_run(_run("r1", RunStatus.RUNNING))
    try:
        await store.delete_run("r1")
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "not terminal" in str(exc)
    assert await store.get_run("r1") is not None
    store.close()


async def test_delete_run_raises_for_unknown_run() -> None:
    store = SQLiteStore.open(":memory:")
    try:
        await store.delete_run("does-not-exist")
        raise AssertionError("expected LookupError")
    except LookupError as exc:
        assert "does-not-exist" in str(exc)
    store.close()


async def test_delete_run_evicts_the_decoded_event_cache() -> None:
    """A deleted run must never be served from ADR-0036's cache after deletion."""
    store = SQLiteStore.open(":memory:")
    await store.create_run(_run("r1", RunStatus.COMPLETED))
    await store.append(Event(run_id="r1", type=EventType.WORKFLOW_COMPLETED, payload={}))
    await store.read_events("r1")  # populate the cache
    assert "r1" in store._event_cache

    await store.delete_run("r1")

    assert "r1" not in store._event_cache
    store.close()


# -- referenced_blob_ids (mark phase) -------------------------------------------------


async def test_referenced_blob_ids_finds_a_spilled_output_ref(tmp_path: Path) -> None:
    data_dir = tmp_path / ".satay"
    data_dir.mkdir()
    store = SQLiteStore.open(db_path(data_dir))
    blobs = BlobStore(blob_dir(data_dir))
    blob_id = await _with_spilled_output(store, blobs, "r1", "x" * 1000)

    assert await store.referenced_blob_ids() == {blob_id}
    store.close()


async def test_referenced_blob_ids_stops_naming_a_blob_once_its_run_is_deleted(
    tmp_path: Path,
) -> None:
    store = SQLiteStore.open(":memory:")
    blobs = BlobStore(tmp_path / "blobs")
    blob_id = await _with_spilled_output(store, blobs, "r1", "y" * 1000)
    assert await store.referenced_blob_ids() == {blob_id}

    await store.delete_run("r1")

    assert await store.referenced_blob_ids() == set()
    store.close()


async def test_referenced_blob_ids_keeps_a_blob_shared_by_a_fork_after_the_source_is_deleted(
    tmp_path: Path,
) -> None:
    """The reference-aware property the whole design exists for (ADR-0004/Q54).

    Must be large enough to actually cross the spill threshold on ``create_fork``'s
    own re-encode of the rehydrated value, not just on the initial synthetic write —
    otherwise the fork's copy re-derives as a small inline value instead of spilling
    again to the same content-addressed id, and the test would prove nothing.
    """
    blobs = BlobStore(tmp_path / "blobs")
    store = SQLiteStore.open(":memory:", blobs=blobs)
    blob_id = await _with_spilled_output(store, blobs, "source", "z" * 300_000)
    await create_fork(
        store,
        source_run_id="source",
        fork_point_seq=1,
        new_run_id="fork",
        now=datetime(2026, 7, 23, tzinfo=UTC),
    )
    await store.set_status("fork", RunStatus.COMPLETED)

    await store.delete_run("source")

    # The fork copied the same blob reference verbatim, so it is still referenced.
    assert await store.referenced_blob_ids() == {blob_id}
    store.close()


# -- collect_garbage (sweep) -----------------------------------------------------------


async def test_collect_garbage_dry_run_reports_without_deleting(tmp_path: Path) -> None:
    data_dir = tmp_path / ".satay"
    data_dir.mkdir()
    store = SQLiteStore.open(db_path(data_dir))
    blobs = BlobStore(blob_dir(data_dir))
    orphan_id = blobs.put(b"orphan" * 1000)
    _age_blob(blobs, orphan_id, seconds_old=3600)

    report = await collect_garbage(store, blobs, apply=False, grace_period_seconds=60)

    assert report.applied is False
    assert report.reclaimable_ids == [orphan_id]
    assert blobs.has(orphan_id)  # nothing actually deleted
    store.close()


async def test_collect_garbage_apply_deletes_unreferenced_old_blobs(tmp_path: Path) -> None:
    data_dir = tmp_path / ".satay"
    data_dir.mkdir()
    store = SQLiteStore.open(db_path(data_dir))
    blobs = BlobStore(blob_dir(data_dir))
    orphan_id = blobs.put(b"orphan" * 1000)
    _age_blob(blobs, orphan_id, seconds_old=3600)

    report = await collect_garbage(store, blobs, apply=True, grace_period_seconds=60)

    assert report.applied is True
    assert report.reclaimable_ids == [orphan_id]
    assert not blobs.has(orphan_id)
    store.close()


async def test_collect_garbage_keeps_a_referenced_blob_even_when_old(tmp_path: Path) -> None:
    data_dir = tmp_path / ".satay"
    data_dir.mkdir()
    store = SQLiteStore.open(db_path(data_dir))
    blobs = BlobStore(blob_dir(data_dir))
    blob_id = await _with_spilled_output(store, blobs, "r1", "x" * 1000)
    _age_blob(blobs, blob_id, seconds_old=3600)

    report = await collect_garbage(store, blobs, apply=True, grace_period_seconds=60)

    assert report.reclaimable_ids == []
    assert blobs.has(blob_id)
    store.close()


async def test_collect_garbage_grace_period_protects_a_recent_unreferenced_blob(
    tmp_path: Path,
) -> None:
    """A blob spilled during/just before the scan is protected regardless of reference."""
    data_dir = tmp_path / ".satay"
    data_dir.mkdir()
    store = SQLiteStore.open(db_path(data_dir))
    blobs = BlobStore(blob_dir(data_dir))
    fresh_id = blobs.put(b"fresh" * 1000)  # unreferenced, but just written

    report = await collect_garbage(store, blobs, apply=True, grace_period_seconds=300)

    assert report.reclaimable_ids == []
    assert fresh_id in report.protected_ids
    assert blobs.has(fresh_id)
    store.close()


def _age_blob(blobs: BlobStore, blob_id: str, *, seconds_old: float) -> None:
    """Backdate a blob file's mtime so it falls outside the default grace period."""
    path = blobs.directory / f"{blob_id}.blob"
    now = path.stat().st_mtime
    os.utime(path, (now - seconds_old, now - seconds_old))
