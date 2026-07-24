"""Integration tests for payload spill (N19, ADR-0004): the one rehydration boundary.

Pure (no FastAPI): drive real workflows through the V1 seam against a **file-backed**
``SQLiteStore`` (which auto-attaches a sibling blob store), and assert spill is invisible
above the store — the value read back and replayed is identical to a never-spilled one —
plus the fork-shares-an-immutable-blob property (ADR-0004/Q54).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from satay import demo
from satay.api.primitives import start
from satay.blobs import is_blob_ref
from satay.config import blob_dir, db_path
from satay.control.api import ControlAPI, ReadAPI
from satay.control.commands import CommandQueue
from satay.control.views import task_detail
from satay.journal.events import EventType, RunStatus
from satay.journal.store import SQLiteStore
from satay.timers import TimerEventWorker


@pytest.fixture(autouse=True)
def _reset() -> None:
    demo.reset_executions()


def _completed_output_json(store: SQLiteStore, run_id: str) -> str:
    row = store._conn.execute(
        "SELECT payload_json FROM events WHERE run_id = ? AND type = ?",
        (run_id, EventType.TASK_COMPLETED.value),
    ).fetchone()
    return str(row["payload_json"])


async def test_large_output_spills_to_a_blob_while_the_journal_holds_a_reference(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / ".satay"
    data_dir.mkdir()
    store = SQLiteStore.open(db_path(data_dir))
    try:
        result = await start(demo.big_output_demo, 7, store=store, run_id="big").result()

        # The workflow result rehydrates to the full large value (spill is invisible).
        assert result["n"] == 7
        assert result["blob"] == "x" * demo.BIG_OUTPUT_SIZE

        # A blob file actually landed on disk under ./.satay/blobs/.
        blob_files = list(blob_dir(data_dir).glob("*.blob"))
        assert blob_files, "expected a spilled blob file on disk"

        # The journal row holds a reference, NOT the inline megastring.
        raw = _completed_output_json(store, "big")
        assert "x" * 1000 not in raw  # the big value is not inlined
        stored = json.loads(raw)
        assert is_blob_ref(stored["output_ref"])
    finally:
        store.close()


async def test_at_threshold_payload_stays_inline(tmp_path: Path) -> None:
    """A modest output stays inline — no blob file, reference resolves inline."""
    data_dir = tmp_path / ".satay"
    data_dir.mkdir()
    store = SQLiteStore.open(db_path(data_dir))
    try:
        await start(demo.demo, 5, store=store, run_id="small").result()
        raw = _completed_output_json(store, "small")
        stored = json.loads(raw)
        assert not is_blob_ref(stored["output_ref"])  # inlined
        assert not list(blob_dir(data_dir).glob("*.blob"))  # nothing spilled
    finally:
        store.close()


async def test_blob_reference_resolves_to_the_same_value_on_replay_and_read(
    tmp_path: Path,
) -> None:
    """The kept integration boundary: a spilled ref resolves identically on replay + read."""
    data_dir = tmp_path / ".satay"
    data_dir.mkdir()
    store = SQLiteStore.open(db_path(data_dir))
    try:
        original = await start(demo.big_output_demo, 3, store=store, run_id="r").result()

        # On replay: re-invoking a terminal run returns the recorded outcome, read back
        # through the store's rehydration path.
        replayed = await start(demo.big_output_demo, 3, store=store, run_id="r").result()
        assert replayed == original

        # On read: the read API rehydrates the spilled output for Studio identically.
        reads = ReadAPI(store)
        detail = await reads.task_detail("r", "big_output_task:0")
        assert detail["output"] == original
    finally:
        store.close()


async def test_fork_shares_the_immutable_blob_and_leaves_the_source_unchanged(
    tmp_path: Path,
) -> None:
    """A fork of a spilled run shares the source blob + ref byte-for-byte (ADR-0004/Q54)."""
    data_dir = tmp_path / ".satay"
    data_dir.mkdir()
    store = SQLiteStore.open(db_path(data_dir))
    queue = CommandQueue()
    control = ControlAPI(store, queue)
    worker = TimerEventWorker(store=store, commands=queue)
    try:
        source_result = await start(demo.big_output_demo, 9, store=store, run_id="src").result()

        # Capture the source's stored reference + blob bytes before forking.
        source_ref_json = _completed_output_json(store, "src")
        source_ref = json.loads(source_ref_json)["output_ref"]
        assert is_blob_ref(source_ref)
        source_blob_id = source_ref["id"]
        source_blob_bytes = (blob_dir(data_dir) / f"{source_blob_id}.blob").read_bytes()
        blob_count_before = len(list(blob_dir(data_dir).glob("*.blob")))

        # Fork keeping through the task's completion, then drive it.
        events = await store.read_events("src")
        fork_point = max(e.seq for e in events if e.type is EventType.TASK_COMPLETED)
        new_id = await control.fork("src", fork_point)
        await worker.tick()

        record = await store.get_run(new_id)
        assert record is not None and record.status is RunStatus.COMPLETED

        # The source's stored reference and blob are byte-for-byte unchanged.
        assert _completed_output_json(store, "src") == source_ref_json
        assert (blob_dir(data_dir) / f"{source_blob_id}.blob").read_bytes() == source_blob_bytes

        # The fork SHARES the same content-addressed blob id (no copy of bytes).
        fork_ref = json.loads(_completed_output_json(store, new_id))["output_ref"]
        assert is_blob_ref(fork_ref)
        assert fork_ref["id"] == source_blob_id
        assert len(list(blob_dir(data_dir).glob("*.blob"))) == blob_count_before

        # And the fork's rehydrated output equals the source's (shared, correct value).
        fork_detail = await task_detail(store, new_id, "big_output_task:0")
        assert fork_detail["output"] == source_result
    finally:
        store.close()
