"""Integration tests for ``Store.read_raw_events`` and its projection (ADR-0044/0045).

``read_raw_events`` is the raw counterpart to ``read_events``: it hands back the payload as
stored, with a spilled blob reference left in place, which is exactly what the ingest-wire
projection needs so blobs travel out of band (ADR-0044 decision 7). These drive a real
file-backed store (so spill is active) and assert observable outcomes only.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from satay.blobs import is_blob_ref
from satay.journal.events import Event, EventType, RunRecord, RunStatus
from satay.journal.store import SQLiteStore
from satay.journal.wire import parse_shipment, project_run


def _run(run_id: str = "r1") -> RunRecord:
    return RunRecord(
        run_id=run_id,
        workflow_name="demo",
        status=RunStatus.COMPLETED,
        code_version="dev:test",
        created_at=datetime(2026, 9, 10, tzinfo=UTC),
        idempotency_key=None,
    )


async def test_raw_read_keeps_blobref_while_read_events_rehydrates(tmp_path: Path) -> None:
    store = SQLiteStore.open(tmp_path / "satay.db")
    await store.create_run(_run())
    big = "x" * 300_000  # over the 256 KiB spill threshold, so it spills to a blob
    await store.append(
        Event(run_id="r1", type=EventType.TASK_COMPLETED, payload={"output_ref": big})
    )

    raw = await store.read_raw_events("r1")
    decoded = await store.read_events("r1")
    store.close()

    # The two read paths diverge exactly here: raw keeps the reference, the normal read
    # rehydrates the bytes for replay and the read views.
    assert is_blob_ref(raw[0].payload["output_ref"])
    assert decoded[0].payload["output_ref"] == big


async def test_read_raw_events_is_seq_ordered_and_projects_to_a_shipment(tmp_path: Path) -> None:
    store = SQLiteStore.open(tmp_path / "satay.db")
    await store.create_run(_run())
    await store.append(
        Event(run_id="r1", type=EventType.WORKFLOW_CREATED, payload={"input_ref": [1, 2]})
    )
    await store.append(
        Event(run_id="r1", type=EventType.WORKFLOW_COMPLETED, payload={"result_ref": "ok"})
    )
    run = await store.get_run("r1")
    assert run is not None
    raw = await store.read_raw_events("r1")
    store.close()

    assert [e.seq for e in raw] == [1, 2]

    shipment = project_run(run, raw, producer="satay/0.1.0", tenant="acme", redaction="off")
    parsed = parse_shipment(shipment)
    assert parsed.run.run_id == "r1"
    assert [e.type for e in parsed.events] == ["WorkflowCreated", "WorkflowCompleted"]
    assert parsed.events[0].payload == {"input_ref": [1, 2]}


async def test_a_spilled_shipment_round_trips_the_reference_not_the_bytes(tmp_path: Path) -> None:
    store = SQLiteStore.open(tmp_path / "satay.db")
    await store.create_run(_run())
    big = "y" * 300_000
    await store.append(
        Event(run_id="r1", type=EventType.TASK_COMPLETED, payload={"output_ref": big})
    )
    run = await store.get_run("r1")
    assert run is not None
    raw = await store.read_raw_events("r1")
    store.close()

    shipment = project_run(run, raw, producer="satay/0.1.0", tenant="acme", redaction="on")
    parsed = parse_shipment(shipment)
    # The reference — not the 300 KB of bytes — is what crosses the wire and comes back.
    assert is_blob_ref(parsed.events[0].payload["output_ref"])
