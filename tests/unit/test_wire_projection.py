"""Unit tests for the ingest-wire projection (ADR-0044/0045, ``satay.journal.wire``).

Pure transform in both directions, so these need no store: hand-built raw events go out and
come back. The properties under test are the contract's: a byte-for-byte round-trip, event
``type``/``payload`` carried opaquely (a non-Satay producer's vocabulary survives), blob
references left in place for the out-of-band channel, and lineage read structurally.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from satay.journal.events import EventType, RawEvent, RunRecord, RunStatus
from satay.journal.wire import (
    SHIPMENT_CONTRACT_VERSION,
    Lineage,
    ShipmentFormatError,
    lineage_of,
    parse_shipment,
    project_run,
)

_TS = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


def _run(run_id: str = "r1", *, status: RunStatus = RunStatus.COMPLETED) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        workflow_name="demo",
        status=status,
        code_version="git:abc123",
        created_at=_TS,
        idempotency_key="local-only-key",
    )


def _event(seq: int, etype: str, payload: dict | None = None) -> RawEvent:
    return RawEvent(
        run_id="r1",
        seq=seq,
        event_id=f"e{seq}",
        type=etype,
        ts=_TS,
        payload=payload or {},
    )


def test_project_run_has_the_six_top_level_keys() -> None:
    shipment = project_run(
        _run(),
        [_event(1, EventType.WORKFLOW_CREATED.value)],
        producer="satay/0.1.0",
        tenant="acme",
        redaction="on",
    )
    assert set(shipment) == {"contract_version", "producer", "tenant", "redaction", "run", "events"}
    assert shipment["contract_version"] == SHIPMENT_CONTRACT_VERSION
    assert shipment["producer"] == "satay/0.1.0"
    assert shipment["tenant"] == "acme"
    assert shipment["redaction"] == "on"


def test_run_header_carries_the_structural_fields_and_not_the_idempotency_key() -> None:
    header = project_run(_run(), [], producer="p", tenant="t", redaction="off")["run"]
    assert header == {
        "run_id": "r1",
        "workflow_name": "demo",
        "status": "completed",
        "code_version": "git:abc123",
        "created_at": _TS.isoformat(),
    }
    # The idempotency key is local-start dedup metadata and never crosses the wire.
    assert "idempotency_key" not in header


def test_round_trip_is_byte_for_byte() -> None:
    events = [
        _event(1, EventType.WORKFLOW_CREATED.value, {"input_ref": [1, 2, 3]}),
        _event(2, EventType.TASK_COMPLETED.value, {"output_ref": {"answer": 42}}),
        _event(3, EventType.WORKFLOW_COMPLETED.value, {"result_ref": "ok"}),
    ]
    shipment = project_run(_run(), events, producer="satay/0.1.0", tenant="acme", redaction="on")
    parsed = parse_shipment(shipment)

    assert parsed.contract_version == SHIPMENT_CONTRACT_VERSION
    assert parsed.producer == "satay/0.1.0"
    assert parsed.tenant == "acme"
    assert parsed.redaction == "on"
    assert parsed.run.run_id == "r1"
    assert parsed.run.status is RunStatus.COMPLETED
    assert parsed.run.idempotency_key is None  # never shipped, so None on the way back
    assert parsed.events == tuple(events)


def test_event_type_and_payload_are_opaque_a_foreign_producer_survives() -> None:
    # A non-Satay producer ships its own event vocabulary and payload shape; the projection
    # neither knows nor rewrites it (ADR-0044 decision 2, the sibei-flow test).
    events = [_event(1, "sibei-flow/PlanStep", {"note": "arbitrary", "nested": {"x": [1]}})]
    parsed = parse_shipment(
        project_run(_run(), events, producer="sibei-flow/2.0", tenant="t", redaction="on")
    )
    assert parsed.events[0].type == "sibei-flow/PlanStep"
    assert parsed.events[0].payload == {"note": "arbitrary", "nested": {"x": [1]}}


def test_blob_reference_is_left_in_place_for_the_out_of_band_channel() -> None:
    ref = {"$satay": "blobref", "id": "a" * 64, "size": 500_000}
    events = [_event(1, EventType.TASK_COMPLETED.value, {"output_ref": ref})]
    shipment = project_run(_run(), events, producer="p", tenant="t", redaction="on")
    # The reference rides in the payload verbatim; the bytes travel separately (decision 7).
    assert shipment["events"][0]["payload"]["output_ref"] == ref
    assert parse_shipment(shipment).events[0].payload["output_ref"] == ref


def test_lineage_is_read_from_the_max_seq_run_forked_and_projected() -> None:
    fork = EventType.RUN_FORKED.value
    events = [
        # A fork-of-a-fork: an ancestor RunForked copied into the prefix, then this run's own.
        _event(1, fork, {"source_run_id": "grandparent", "fork_point_seq": 2}),
        _event(2, EventType.WORKFLOW_CREATED.value, {}),
        _event(3, fork, {"source_run_id": "parent", "fork_point_seq": 5}),
    ]
    assert lineage_of(events) == Lineage(source_run_id="parent", fork_point_seq=5)

    header = project_run(_run(), events, producer="p", tenant="t", redaction="on")["run"]
    assert header["lineage"] == {"source_run_id": "parent", "fork_point_seq": 5}
    assert parse_shipment(
        project_run(_run(), events, producer="p", tenant="t", redaction="on")
    ).lineage == Lineage(source_run_id="parent", fork_point_seq=5)


def test_an_unforked_run_has_no_lineage() -> None:
    events = [_event(1, EventType.WORKFLOW_CREATED.value)]
    assert lineage_of(events) is None
    shipment = project_run(_run(), events, producer="p", tenant="t", redaction="off")
    assert "lineage" not in shipment["run"]
    assert parse_shipment(shipment).lineage is None


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda s: s.pop("run"), id="missing-run"),
        pytest.param(lambda s: s.pop("events"), id="missing-events"),
        pytest.param(lambda s: s.__setitem__("events", "not-a-list"), id="events-not-a-list"),
        pytest.param(lambda s: s["run"].pop("run_id"), id="missing-run-id"),
        pytest.param(lambda s: s.__setitem__("redaction", "maybe"), id="bad-redaction"),
        pytest.param(lambda s: s["events"][0].__setitem__("seq", "1"), id="seq-not-an-int"),
        # A bad enum value or timestamp must surface as ShipmentFormatError, not the bare
        # ValueError that RunStatus()/datetime.fromisoformat raise (the documented contract).
        pytest.param(lambda s: s["run"].__setitem__("status", "bananas"), id="unknown-status"),
        pytest.param(lambda s: s["run"].__setitem__("created_at", "nope"), id="bad-created-at"),
        pytest.param(lambda s: s["events"][0].__setitem__("ts", "nope"), id="bad-event-ts"),
    ],
)
def test_a_malformed_shipment_is_rejected(mutate) -> None:
    shipment = project_run(
        _run(),
        [_event(1, EventType.WORKFLOW_CREATED.value)],
        producer="p",
        tenant="t",
        redaction="on",
    )
    mutate(shipment)
    with pytest.raises(ShipmentFormatError):
        parse_shipment(shipment)
