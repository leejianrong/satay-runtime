"""Unit tests for the HTTP+NDJSON transport (ADR-0044/0045, ``satay.ingest.http``).

Driven through :class:`httpx.MockTransport`, so they assert the request the shipper *sends*
(method, path, bearer auth, gzip NDJSON framing) and how it reads the plane's reply —
without a real server. One stateful mock ties the transport back to ``ship_run``.
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from satay.blobs import BlobStore
from satay.config import BLOB_DIR_NAME
from satay.ingest import BackpressureError, ship_run
from satay.ingest.http import HttpTransport, _encode_ndjson
from satay.journal.events import Event, EventType, RawEvent, RunRecord, RunStatus
from satay.journal.store import SQLiteStore
from satay.journal.wire import project_run

BASE = "https://plane.example"


def _transport(handler: Callable[[httpx.Request], httpx.Response], **kwargs) -> HttpTransport:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return HttpTransport(BASE, token="secret-token", client=client, **kwargs)


def _shipment() -> dict:
    run = RunRecord(
        run_id="r1",
        workflow_name="demo",
        status=RunStatus.COMPLETED,
        code_version="git:abc",
        created_at=datetime(2026, 9, 10, tzinfo=UTC),
        idempotency_key=None,
    )
    events = [
        RawEvent(
            run_id="r1",
            seq=1,
            event_id="e1",
            type="WorkflowCreated",
            ts=datetime(2026, 9, 10, tzinfo=UTC),
            payload={"input_ref": [1]},
        ),
        RawEvent(
            run_id="r1",
            seq=2,
            event_id="e2",
            type="WorkflowCompleted",
            ts=datetime(2026, 9, 10, tzinfo=UTC),
            payload={"result_ref": "ok"},
        ),
    ]
    return project_run(run, events, producer="satay/0.1.0", tenant="acme", redaction="on")


async def test_current_ack_reads_the_ack_seq_and_sends_the_bearer_token() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        seen["path"] = request.url.path
        seen["tenant"] = request.url.params.get("tenant")
        return httpx.Response(200, json={"ack_seq": 3})

    async with _transport(handler) as transport:
        assert await transport.current_ack("acme", "r1") == 3
    assert seen["auth"] == "Bearer secret-token"
    assert seen["path"] == "/v1/runs/r1/ack"
    assert seen["tenant"] == "acme"


async def test_current_ack_is_zero_when_the_plane_has_never_seen_the_run() -> None:
    async with _transport(lambda request: httpx.Response(404)) as transport:
        assert await transport.current_ack("acme", "r1") == 0


async def test_send_posts_gzip_ndjson_header_then_events() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["encoding"] = request.headers.get("Content-Encoding")
        lines = gzip.decompress(request.content).decode().splitlines()
        captured["lines"] = lines
        return httpx.Response(200, json={"ack_seq": 2})

    async with _transport(handler) as transport:
        ack = await transport.send(_shipment())

    assert ack.ack_seq == 2
    assert captured["method"] == "POST"
    assert captured["path"] == "/v1/runs/r1/events"
    assert captured["encoding"] == "gzip"
    header = json.loads(captured["lines"][0])
    assert "events" not in header  # the header line carries everything but the events
    assert header["tenant"] == "acme" and header["redaction"] == "on"
    seqs = [json.loads(line)["seq"] for line in captured["lines"][1:]]
    assert seqs == [1, 2]


async def test_blob_present_maps_head_status() -> None:
    async with _transport(lambda request: httpx.Response(200)) as present:
        assert await present.blob_present("sha1") is True
    async with _transport(lambda request: httpx.Response(404)) as absent:
        assert await absent.blob_present("sha1") is False


async def test_put_blob_uploads_the_bytes() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["body"] = request.content
        return httpx.Response(200)

    async with _transport(handler) as transport:
        await transport.put_blob("abc123", b"blob-bytes")
    assert captured["method"] == "PUT"
    assert captured["path"] == "/v1/blobs/abc123"
    assert captured["body"] == b"blob-bytes"


async def test_a_429_becomes_backpressure_carrying_retry_after() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "1.5"})

    async with _transport(handler) as transport:
        with pytest.raises(BackpressureError) as excinfo:
            await transport.send(_shipment())
    assert excinfo.value.retry_after == 1.5


async def test_a_429_without_retry_after_leaves_it_none() -> None:
    async with _transport(lambda request: httpx.Response(429)) as transport:
        with pytest.raises(BackpressureError) as excinfo:
            await transport.blob_present("sha1")
    assert excinfo.value.retry_after is None


async def test_a_plane_5xx_raises_rather_than_being_swallowed() -> None:
    async with _transport(lambda request: httpx.Response(500)) as transport:
        with pytest.raises(httpx.HTTPStatusError):
            await transport.send(_shipment())


def test_encode_ndjson_is_header_line_then_one_event_per_line() -> None:
    body = _encode_ndjson(_shipment()).decode()
    lines = body.splitlines()
    assert len(lines) == 3  # header + 2 events
    assert "events" not in json.loads(lines[0])
    assert [json.loads(line)["seq"] for line in lines[1:]] == [1, 2]


def _stateful_plane() -> tuple[Callable[[httpx.Request], httpx.Response], dict]:
    """A minimal in-memory plane over HTTP, honouring the ADR-0044 ingest invariants."""
    state: dict = {"events": {}, "blobs": set()}

    def _ack() -> int:
        ack = 0
        for seq in sorted(state["events"]):
            if seq == ack + 1:
                ack = seq
            else:
                break
        return ack

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/ack"):
            return httpx.Response(200, json={"ack_seq": _ack()})
        if request.method == "POST" and path.endswith("/events"):
            for line in gzip.decompress(request.content).decode().splitlines()[1:]:
                event = json.loads(line)
                state["events"][event["seq"]] = event
            return httpx.Response(200, json={"ack_seq": _ack()})
        if request.method == "HEAD" and "/blobs/" in path:
            blob_id = path.rsplit("/", 1)[1]
            return httpx.Response(200 if blob_id in state["blobs"] else 404)
        if request.method == "PUT" and "/blobs/" in path:
            state["blobs"].add(path.rsplit("/", 1)[1])
            return httpx.Response(200)
        return httpx.Response(404)  # pragma: no cover - defensive

    return handler, state


async def test_ship_run_over_the_http_transport_against_a_stateful_plane(tmp_path: Path) -> None:
    store = SQLiteStore.open(tmp_path / "satay.db")
    await store.create_run(
        RunRecord(
            run_id="r1",
            workflow_name="demo",
            status=RunStatus.COMPLETED,
            code_version="dev:test",
            created_at=datetime(2026, 9, 10, tzinfo=UTC),
            idempotency_key=None,
        )
    )
    big = "b" * 300_000  # spills, so the blob channel is exercised end to end
    await store.append(
        Event(run_id="r1", type=EventType.TASK_COMPLETED, payload={"output_ref": big})
    )
    await store.append(Event(run_id="r1", type=EventType.WORKFLOW_COMPLETED, payload={"r": 1}))

    handler, state = _stateful_plane()
    blobs = BlobStore(tmp_path / BLOB_DIR_NAME)
    async with _transport(handler, requires_redaction=False) as transport:
        result = await ship_run(
            "r1",
            store=store,
            transport=transport,
            producer="satay/0.1.0",
            tenant="acme",
            blobs=blobs,
        )
    store.close()

    assert result.events_shipped == 2
    assert result.ack_seq == 2
    assert result.blobs_uploaded == 1
    assert sorted(state["events"]) == [1, 2]
    assert len(state["blobs"]) == 1
