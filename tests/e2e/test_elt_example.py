"""E2E: the ELT example proves what its printed ledger claims (KAN-462).

``tests/e2e/test_examples.py`` already sweeps every file under ``examples/`` for "exits 0
and leaves a coherent journal". This module asserts the three properties the ELT pipeline
exists to demonstrate, and the one limitation it exists to be honest about:

* a worker death mid-load reuses the sources already loaded and re-runs only the rest;
* the keyed loader survives an ambiguous completion with one warehouse row per record,
  while the loader that ignores ``ctx.idempotency_key`` duplicates every record;
* an over-threshold task output leaves a **blob reference** on the journal row and the
  full value on the way back out;
* fan-out is fail-fast (ADR-0020): one bad source fails the run while its siblings'
  results sit completed-and-unreachable on the journal.

Observable outcomes only (ADR-0011): the printed ledger, the journal, run status, the
warehouse table the example writes, and the blob files on disk.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

from satay.blobs import SPILL_THRESHOLD_BYTES
from satay.config import DATA_DIR_ENV_VAR, blob_dir, db_path
from satay.journal.events import Event, EventType, RunStatus
from satay.journal.store import SQLiteStore

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = REPO_ROOT / "examples" / "elt_pipeline_demo.py"

#: What the example loads, and how many records each source holds.
EXPECTED_RECORDS = {
    "crm-contacts": 3,
    "orders": 4,
    "clickstream": 1,
    "billing": 2,
    "inventory": 2,
}

#: The source whose load loses its warehouse ack and therefore runs twice.
LOST_ACK_SOURCE = "orders"

#: The over-threshold source: its extract output spills to a content-addressed blob.
WIDE_SOURCE = "clickstream"


@pytest.fixture(scope="module")
def elt_run(tmp_path_factory: pytest.TempPathFactory) -> tuple[str, Path]:
    """Run the example once for the whole module and hand back its stdout + data dir."""
    data_dir = tmp_path_factory.mktemp("elt")
    env = {**os.environ, "PYTHONUNBUFFERED": "1", DATA_DIR_ENV_VAR: str(data_dir)}
    proc = subprocess.run(
        [sys.executable, str(EXAMPLE)],
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO_ROOT,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, (
        f"example exited {proc.returncode}\n--- stdout ---\n{proc.stdout[-4000:]}\n"
        f"--- stderr ---\n{proc.stderr[-4000:]}"
    )
    return proc.stdout, data_dir


async def runs_by_workflow(data_dir: Path) -> dict[str, tuple[str, list[Event]]]:
    """Every run in the data dir as ``{workflow_name: (status, events)}``."""
    store = SQLiteStore.open(db_path(data_dir))
    try:
        out: dict[str, tuple[str, list[Event]]] = {}
        for run_id in await store.list_runs():
            record = await store.get_run(run_id)
            assert record is not None
            out[record.workflow_name] = (record.status.value, list(await store.read_events(run_id)))
        return out
    finally:
        store.close()


def keys_completed(events: list[Event], task_name: str) -> list[str]:
    """The fan-out key of every item of ``task_name`` whose result is on the journal."""
    return [
        event.payload["key"]
        for event in events
        if event.type is EventType.TASK_COMPLETED and event.payload.get("task_name") == task_name
    ]


def attempts(events: list[Event], task_name: str, key: str) -> list[int]:
    """Attempt numbers recorded for one keyed durable call, in order."""
    return [
        event.payload["attempt"]
        for event in events
        if event.type is EventType.TASK_ATTEMPT_STARTED
        and event.payload.get("task_name") == task_name
        and event.payload.get("key") == key
    ]


def warehouse_rows(data_dir: Path) -> dict[str, tuple[int, int]]:
    """Per source id: total rows written, and how many distinct records they cover."""
    conn = sqlite3.connect(data_dir / "warehouse.db")
    try:
        rows = conn.execute(
            "SELECT source_id, COUNT(*), COUNT(DISTINCT record_id) "
            "FROM warehouse GROUP BY source_id"
        ).fetchall()
    finally:
        conn.close()
    return {source_id: (total, distinct) for source_id, total, distinct in rows}


def raw_payload(data_dir: Path, task_name: str, key: str) -> dict[str, object]:
    """The literal stored ``payload_json`` for one keyed ``TaskCompleted``, un-rehydrated."""
    conn = sqlite3.connect(f"file:{db_path(data_dir)}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT payload_json FROM events WHERE type = ?", (EventType.TASK_COMPLETED.value,)
        ).fetchall()
    finally:
        conn.close()
    for (payload_json,) in rows:
        payload = json.loads(payload_json)
        if payload.get("task_name") == task_name and payload.get("key") == key:
            return dict(payload)
    raise AssertionError(f"no stored TaskCompleted row for {task_name}/{key}")


# -- 1: crash mid-load, resume, reuse --------------------------------------------------


async def test_pipeline_resumes_mid_load_and_reloads_nothing(elt_run: tuple[str, Path]) -> None:
    """One crash mid-load; every source ends up extracted, transformed and loaded once."""
    stdout, data_dir = elt_run
    status, events = (await runs_by_workflow(data_dir))["elt_pipeline"]

    assert status == RunStatus.COMPLETED.value
    assert sum(1 for e in events if e.type is EventType.WORKFLOW_RESUMED) == 1  # one ⚡

    for stage in ("extract", "transform", "load"):
        keys = keys_completed(events, stage)
        assert sorted(keys) == sorted(EXPECTED_RECORDS), f"{stage} did not cover every source"
        assert len(keys) == len(set(keys)), f"{stage} recorded a key twice — reuse is broken"

    # The ledger has to actually name what was reused rather than re-run.
    assert stdout.count("(REUSED)") == 2
    assert "durably loaded before the crash: ['crm-contacts', 'orders']" in stdout
    assert "15 durable calls, 16 task bodies executed" in stdout


# -- 2: idempotent load ----------------------------------------------------------------


async def test_keyed_load_survives_an_ambiguous_completion(elt_run: tuple[str, Path]) -> None:
    """The lost ack forces a second physical attempt that writes no second row."""
    stdout, data_dir = elt_run
    _, events = (await runs_by_workflow(data_dir))["elt_pipeline"]

    assert attempts(events, "load", LOST_ACK_SOURCE) == [1, 2], "the lost ack did not retry"
    failed = [
        event.payload
        for event in events
        if event.type is EventType.TASK_ATTEMPT_FAILED and event.payload.get("task_name") == "load"
    ]
    assert len(failed) == 1
    assert failed[0]["error"]["type"] == "ConnectionError"

    rows = warehouse_rows(data_dir)
    for source_id, records in EXPECTED_RECORDS.items():
        total, distinct = rows[source_id]
        assert (total, distinct) == (records, records), (
            f"{source_id}: {total} warehouse rows for {distinct} records — the "
            f"idempotency key did not hold"
        )
    assert "0 row(s) written, 4 already keyed in" in stdout


async def test_unkeyed_load_double_inserts_the_same_records(elt_run: tuple[str, Path]) -> None:
    """The wrong outcome, made visible: ignoring ctx.idempotency_key duplicates everything."""
    stdout, data_dir = elt_run
    status, _ = (await runs_by_workflow(data_dir))["careless_load"]
    assert status == RunStatus.COMPLETED.value  # it "succeeded" — that is the trap

    total, distinct = warehouse_rows(data_dir)["orders-careless"]
    assert distinct == EXPECTED_RECORDS[LOST_ACK_SOURCE]
    assert total == 2 * distinct, "the unkeyed loader was supposed to double-insert"
    assert "every record duplicated" in stdout
    assert "same lost ack, no damage" in stdout


# -- 3: blob spill ---------------------------------------------------------------------


async def test_wide_source_spills_to_a_blob_and_reads_back_whole(
    elt_run: tuple[str, Path],
) -> None:
    """The journal row holds a reference; every read path hands back the full value."""
    stdout, data_dir = elt_run
    _, events = (await runs_by_workflow(data_dir))["elt_pipeline"]

    stored = raw_payload(data_dir, "extract", WIDE_SOURCE)
    reference = stored["output_ref"]
    assert isinstance(reference, dict)
    assert reference["$satay"] == "blobref"
    assert isinstance(reference["size"], int)
    assert reference["size"] > SPILL_THRESHOLD_BYTES
    # The row itself is tiny: it carries an address, not the payload.
    assert len(json.dumps(stored)) < 1024

    blob = blob_dir(data_dir) / f"{reference['id']}.blob"
    assert blob.exists(), "the reference points at no blob on disk"
    assert blob.stat().st_size == reference["size"]

    # Read back through Satay (blob rehydrated, spill invisible above the store).
    recorded = next(
        event.payload["output_ref"]
        for event in events
        if event.type is EventType.TASK_COMPLETED
        and event.payload.get("task_name") == "extract"
        and event.payload.get("key") == WIDE_SOURCE
    )
    assert len(recorded["text"]) > SPILL_THRESHOLD_BYTES

    # And the same value comes out of handle.result(), which the demo prints beside it.
    sha_lines = [line for line in stdout.splitlines() if "sha " in line and "chars," in line]
    assert len(sha_lines) == 2, sha_lines
    assert sha_lines[0].split("sha ")[1] == sha_lines[1].split("sha ")[1]


# -- 4/5: fail-fast fan-out, and the workaround ----------------------------------------


async def test_one_bad_source_fails_the_whole_fan_out(elt_run: tuple[str, Path]) -> None:
    """Fail-fast (ADR-0020): siblings complete, the run fails, their results are stranded."""
    stdout, data_dir = elt_run
    status, events = (await runs_by_workflow(data_dir))["strict_extract"]

    assert status == RunStatus.FAILED.value
    tally = Counter(event.type for event in events)
    assert tally[EventType.WORKFLOW_FAILED] == 1
    assert tally[EventType.TASK_ATTEMPT_FAILED] == 1
    # The whole point: the good siblings *did* finish and *are* on the journal.
    survivors = keys_completed(events, "extract_strictly")
    assert sorted(survivors) == sorted(EXPECTED_RECORDS)
    assert WIDE_SOURCE in survivors, "the expensive extract completed and was still discarded"

    failure = next(e.payload for e in events if e.type is EventType.WORKFLOW_FAILED)
    assert failure["error"]["type"] == "ValueError"
    assert "characters of finished work" in stdout


async def test_outcome_returning_task_is_the_available_workaround(
    elt_run: tuple[str, Path],
) -> None:
    """A task that reports instead of raising keeps the fan-out — and the run — alive."""
    stdout, data_dir = elt_run
    status, events = (await runs_by_workflow(data_dir))["resilient_extract"]

    assert status == RunStatus.COMPLETED.value
    keys = keys_completed(events, "extract_outcome")
    assert len(keys) == len(EXPECTED_RECORDS) + 1  # the bad source completes too, as an Outcome
    assert not any(event.type is EventType.WORKFLOW_FAILED for event in events)
    assert "quarantined: ledger-eu" in stdout
    assert "5/6 sources survived" in stdout


# -- the runs the example is expected to leave behind ----------------------------------


async def test_example_leaves_exactly_the_four_runs_it_narrates(
    elt_run: tuple[str, Path],
) -> None:
    _, data_dir = elt_run
    runs = await runs_by_workflow(data_dir)
    assert set(runs) == {"elt_pipeline", "careless_load", "strict_extract", "resilient_extract"}
    assert [name for name, (status, _) in runs.items() if status == RunStatus.FAILED.value] == [
        "strict_extract"
    ]
