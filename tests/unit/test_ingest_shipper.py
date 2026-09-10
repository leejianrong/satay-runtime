"""Unit tests for the producer shipper (ADR-0044/0045, ``satay.ingest.shipper``).

The shipper is pure control flow over a :class:`~satay.ingest.Transport` seam, so these
drive it against an in-memory fake plane and a real ``SQLiteStore``, asserting observable
outcomes: what the plane ends up holding, the redaction gate, blob dedupe, incremental
resume, and backpressure waiting. No network, no real time.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from satay.blobs import BlobStore
from satay.config import BLOB_DIR_NAME
from satay.ingest import (
    BackpressureError,
    MissingBlobError,
    RedactionRequiredError,
    ShipmentAck,
    UnknownRunError,
    ship_run,
)
from satay.journal.events import Event, EventType, RunRecord, RunStatus
from satay.journal.store import SQLiteStore
from satay.testing.rng import SystemRng


class FakePlane:
    """An in-memory stand-in for the ingest plane, honouring the ADR-0044 invariants."""

    def __init__(
        self,
        *,
        requires_redaction: bool = False,
        backpressure_sends: int = 0,
        retry_after: float | None = None,
    ) -> None:
        self.requires_redaction = requires_redaction
        self.runs: dict[tuple[str, str], dict[str, Any]] = {}
        self.blobs: dict[str, bytes] = {}
        self.blob_head_calls = 0
        self._bp_remaining = backpressure_sends
        self._retry_after = retry_after

    def _contiguous_ack(self, tenant: str, run_id: str) -> int:
        run = self.runs.get((tenant, run_id))
        if run is None:
            return 0
        ack = 0
        for seq in sorted(run["events"]):
            if seq == ack + 1:
                ack = seq
            else:
                break
        return ack

    async def current_ack(self, tenant: str, run_id: str) -> int:
        return self._contiguous_ack(tenant, run_id)

    async def send(self, shipment: Mapping[str, Any]) -> ShipmentAck:
        if self._bp_remaining > 0:
            self._bp_remaining -= 1
            raise BackpressureError("over quota", retry_after=self._retry_after)
        tenant = shipment["tenant"]
        run_id = shipment["run"]["run_id"]
        run = self.runs.setdefault((tenant, run_id), {"header": None, "events": {}})
        run["header"] = shipment["run"]
        for event in shipment["events"]:
            seq = event["seq"]
            existing = run["events"].get(seq)
            if existing is not None and existing["event_id"] != event["event_id"]:
                raise AssertionError(f"divergent event_id at seq {seq} (append-only violated)")
            run["events"][seq] = event
        return ShipmentAck(ack_seq=self._contiguous_ack(tenant, run_id))

    async def blob_present(self, blob_id: str) -> bool:
        self.blob_head_calls += 1
        return blob_id in self.blobs

    async def put_blob(self, blob_id: str, data: bytes) -> None:
        self.blobs[blob_id] = data


def _run(run_id: str = "r1") -> RunRecord:
    from datetime import UTC, datetime

    return RunRecord(
        run_id=run_id,
        workflow_name="demo",
        status=RunStatus.RUNNING,
        code_version="dev:test",
        created_at=datetime(2026, 9, 10, tzinfo=UTC),
        idempotency_key=None,
    )


async def _seed(store: SQLiteStore, run_id: str, count: int) -> None:
    await store.create_run(_run(run_id))
    for _ in range(count):
        await store.append(
            Event(run_id=run_id, type=EventType.TASK_COMPLETED, payload={"output_ref": "ok"})
        )


async def test_ships_a_whole_run_and_the_plane_holds_it() -> None:
    store = SQLiteStore.open(":memory:")
    await _seed(store, "r1", 3)
    plane = FakePlane()

    result = await ship_run(
        "r1", store=store, transport=plane, producer="satay/0.1.0", tenant="acme"
    )
    store.close()

    assert result.events_shipped == 3
    assert result.first_seq_shipped == 1
    assert result.ack_seq == 3
    assert result.redaction == "off"
    assert sorted(plane.runs[("acme", "r1")]["events"]) == [1, 2, 3]
    assert plane.runs[("acme", "r1")]["header"]["run_id"] == "r1"


async def test_unknown_run_is_rejected() -> None:
    store = SQLiteStore.open(":memory:")
    with pytest.raises(UnknownRunError):
        await ship_run("nope", store=store, transport=FakePlane(), producer="p", tenant="t")
    store.close()


async def test_custodial_endpoint_refuses_an_unredacted_store() -> None:
    store = SQLiteStore.open(":memory:")  # write-redaction off by default
    await _seed(store, "r1", 1)
    with pytest.raises(RedactionRequiredError):
        await ship_run(
            "r1",
            store=store,
            transport=FakePlane(requires_redaction=True),
            producer="p",
            tenant="t",
        )
    store.close()


async def test_a_redacted_store_ships_to_a_custodial_endpoint() -> None:
    store = SQLiteStore.open(":memory:", write_redaction="on")
    await _seed(store, "r1", 2)
    result = await ship_run(
        "r1", store=store, transport=FakePlane(requires_redaction=True), producer="p", tenant="t"
    )
    store.close()
    assert result.redaction == "on"
    assert result.events_shipped == 2


async def test_incremental_resume_ships_only_the_tail() -> None:
    store = SQLiteStore.open(":memory:")
    await _seed(store, "r1", 2)
    plane = FakePlane()

    first = await ship_run("r1", store=store, transport=plane, producer="p", tenant="t")
    assert first.events_shipped == 2 and first.ack_seq == 2

    # The run advances; a second ship sends only what the plane has not acknowledged.
    await store.append(Event(run_id="r1", type=EventType.WORKFLOW_COMPLETED, payload={"r": 1}))
    second = await ship_run("r1", store=store, transport=plane, producer="p", tenant="t")
    store.close()

    assert second.first_seq_shipped == 3
    assert second.events_shipped == 1
    assert second.ack_seq == 3
    assert sorted(plane.runs[("t", "r1")]["events"]) == [1, 2, 3]


async def test_re_shipping_an_unchanged_run_is_a_no_op() -> None:
    store = SQLiteStore.open(":memory:")
    await _seed(store, "r1", 2)
    plane = FakePlane()
    await ship_run("r1", store=store, transport=plane, producer="p", tenant="t")

    again = await ship_run("r1", store=store, transport=plane, producer="p", tenant="t")
    store.close()
    assert again.events_shipped == 0
    assert again.first_seq_shipped is None
    assert again.ack_seq == 2


async def test_a_spilled_blob_uploads_once_then_is_deduped(tmp_path: Path) -> None:
    store = SQLiteStore.open(tmp_path / "satay.db")
    await store.create_run(_run("r1"))
    big = "z" * 300_000  # spills to a content-addressed blob
    await store.append(
        Event(run_id="r1", type=EventType.TASK_COMPLETED, payload={"output_ref": big})
    )
    # A second event reusing the same value re-derives the same blob id.
    await store.append(
        Event(run_id="r1", type=EventType.TASK_COMPLETED, payload={"output_ref": big})
    )

    blobs = BlobStore(tmp_path / BLOB_DIR_NAME)
    plane = FakePlane()
    result = await ship_run(
        "r1", store=store, transport=plane, producer="p", tenant="t", blobs=blobs
    )
    store.close()

    # One distinct blob, uploaded once despite two references.
    assert result.blobs_uploaded == 1
    assert len(plane.blobs) == 1


async def test_a_blob_the_plane_already_holds_is_not_re_uploaded(tmp_path: Path) -> None:
    store = SQLiteStore.open(tmp_path / "satay.db")
    await store.create_run(_run("r1"))
    big = "w" * 300_000
    await store.append(
        Event(run_id="r1", type=EventType.TASK_COMPLETED, payload={"output_ref": big})
    )
    blobs = BlobStore(tmp_path / BLOB_DIR_NAME)
    plane = FakePlane()

    first = await ship_run(
        "r1", store=store, transport=plane, producer="p", tenant="t", blobs=blobs
    )
    assert first.blobs_uploaded == 1

    # Append and ship again: the existing blob is HEAD-checked and skipped.
    await store.append(Event(run_id="r1", type=EventType.WORKFLOW_COMPLETED, payload={"r": big}))
    second = await ship_run(
        "r1", store=store, transport=plane, producer="p", tenant="t", blobs=blobs
    )
    store.close()
    assert second.blobs_uploaded == 0


async def test_a_referenced_blob_with_no_blob_store_is_an_error(tmp_path: Path) -> None:
    store = SQLiteStore.open(tmp_path / "satay.db")
    await store.create_run(_run("r1"))
    await store.append(
        Event(run_id="r1", type=EventType.TASK_COMPLETED, payload={"output_ref": "q" * 300_000})
    )
    with pytest.raises(MissingBlobError):
        # blobs omitted, but the run spilled — the plane would get an unresolvable reference.
        await ship_run("r1", store=store, transport=FakePlane(), producer="p", tenant="t")
    store.close()


async def test_backpressure_is_waited_out_with_retry_after() -> None:
    store = SQLiteStore.open(":memory:")
    await _seed(store, "r1", 1)
    plane = FakePlane(backpressure_sends=2, retry_after=0.25)
    slept: list[float] = []

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)

    result = await ship_run(
        "r1",
        store=store,
        transport=plane,
        producer="p",
        tenant="t",
        rng=SystemRng(),
        sleep=fake_sleep,
    )
    store.close()

    # Two 429s, so two waits, each honouring the plane's Retry-After hint; then it lands.
    assert slept == [0.25, 0.25]
    assert result.events_shipped == 1


async def test_persistent_backpressure_eventually_gives_up() -> None:
    store = SQLiteStore.open(":memory:")
    await _seed(store, "r1", 1)
    plane = FakePlane(backpressure_sends=99, retry_after=0.0)

    async def fake_sleep(delay: float) -> None:
        return None

    with pytest.raises(BackpressureError):
        await ship_run(
            "r1",
            store=store,
            transport=plane,
            producer="p",
            tenant="t",
            sleep=fake_sleep,
            max_backpressure_retries=3,
        )
    store.close()
