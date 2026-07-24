"""End-to-end tests for the V5 HTTP surface — driven through FastAPI's TestClient.

These exercise the real HTTP transport: routing, status codes, request-body validation,
the ADR-0014 security guard (token + ``Origin``/``Host`` allow-list, non-loopback bind
refusal), and the write-then-poll demo (start / send_event / cancel over HTTP applied by
the worker's poll loop). They require the ``satay[studio]`` extra; under the plain dev
env the module is skipped rather than erroring, so the pure tiers still run everywhere.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from satay import demo
from satay.api.primitives import start
from satay.control.commands import CommandQueue
from satay.control.security import (
    TOKEN_HEADER,
    NonLoopbackBindError,
    SecurityPolicy,
)
from satay.control.server import create_app, serve
from satay.journal.events import RunStatus
from satay.journal.store import SQLiteStore
from satay.testing.clock import ManualClock
from satay.timers import TimerEventWorker

TOKEN = "test-session-token"


@pytest.fixture(autouse=True)
def _reset() -> None:
    demo.reset_executions()


def _app(store: SQLiteStore, queue: CommandQueue) -> object:
    return create_app(store=store, command_queue=queue, security=SecurityPolicy(token=TOKEN))


def _client(app: object, *, with_token: bool = True) -> TestClient:
    headers = {TOKEN_HEADER: TOKEN} if with_token else {}
    return TestClient(app, base_url="http://localhost", headers=headers)  # type: ignore[arg-type]


# -- the demo: start / send_event / resume / cancel over HTTP --------------------


async def test_start_send_event_and_resume_over_http() -> None:
    clock = ManualClock()
    store = SQLiteStore.open(":memory:")
    queue = CommandQueue()
    worker = TimerEventWorker(store=store, clock=clock, commands=queue)

    with _client(_app(store, queue)) as client:
        resp = client.post("/runs", json={"workflow": "review_demo", "input": 0})
        assert resp.status_code == 202
        run_id = resp.json()["run_id"]

        await worker.tick()  # worker applies the start; the run parks on the event wait
        assert (await store.get_run(run_id)).status is RunStatus.WAITING

        resp = client.post(
            f"/runs/{run_id}/events",
            json={
                "event_type": "satay.demo.ReviewDecision",
                "key": demo.REVIEW_KEY,
                "payload": {"approved": True, "reviewer": "alice"},
            },
        )
        assert resp.status_code == 202

        await worker.tick()  # the HTTP event lands in the V3 inbox and resumes the run
        timeline = client.get(f"/runs/{run_id}/timeline").json()
        assert timeline["status"] == "completed"
        assert "ExternalEventReceived" in [e["type"] for e in timeline["events"]]
    store.close()


async def test_cancel_over_http_reaches_workflow_cancelled() -> None:
    clock = ManualClock()
    store = SQLiteStore.open(":memory:")
    queue = CommandQueue()
    worker = TimerEventWorker(store=store, clock=clock, commands=queue)

    with _client(_app(store, queue)) as client:
        run_id = client.post("/runs", json={"workflow": "review_demo", "input": 0}).json()["run_id"]
        await worker.tick()  # parks
        assert client.post(f"/runs/{run_id}/cancel").status_code == 202
        await worker.tick()  # cancel applied within one poll interval
        assert (await store.get_run(run_id)).status is RunStatus.CANCELLED
        assert client.get(f"/runs/{run_id}/timeline").json()["status"] == "cancelled"
    store.close()


# -- read endpoints over HTTP ----------------------------------------------------


async def test_read_endpoints_return_the_json_contract() -> None:
    store = SQLiteStore.open(":memory:")
    queue = CommandQueue()
    await start(demo.parent_workflow, 2, store=store, run_id="p1").result()
    await start(demo.demo, 5, store=store, run_id="d1").result()

    with _client(_app(store, queue)) as client:
        runs = client.get("/runs").json()["runs"]
        assert {"p1", "d1"} <= {r["run_id"] for r in runs}

        tree = client.get("/runs/p1/tree").json()
        assert any(n["kind"] == "child" for n in tree["nodes"])

        detail = client.get("/runs/d1/tasks/step_one:0").json()
        assert detail["task_name"] == "step_one"
        assert detail["output"] == 6  # step_one(5)

        cmp = client.get("/runs/d1/compare", params={"to": "d1"}).json()
        assert all(row["aligned"] for row in cmp["rows"])
    store.close()


async def test_redaction_applies_over_the_http_read_path() -> None:
    from dataclasses import dataclass

    from satay.api.decorators import task, workflow

    @dataclass(frozen=True)
    class HttpSecret:
        api_key: str

    @task()
    async def http_secret_task(payload: HttpSecret) -> dict[str, str]:
        return {"token": "leaked-over-http"}

    @workflow
    async def http_secret_wf(payload: HttpSecret) -> dict[str, str]:
        return await http_secret_task(payload)

    store = SQLiteStore.open(":memory:")
    queue = CommandQueue()
    await start(
        http_secret_wf, HttpSecret(api_key="topsecret-http"), store=store, run_id="h1"
    ).result()

    with _client(_app(store, queue)) as client:
        body = client.get("/runs/h1/timeline").text
        assert "topsecret-http" not in body
        assert "leaked-over-http" not in body
        assert "REDACTED" in body
    store.close()


# -- security guard (ADR-0014) ---------------------------------------------------


def test_missing_or_invalid_token_is_rejected() -> None:
    store = SQLiteStore.open(":memory:")
    queue = CommandQueue()
    app = _app(store, queue)
    with _client(app, with_token=False) as client:
        assert client.get("/runs").status_code == 401
        assert client.get("/runs", headers={TOKEN_HEADER: "wrong"}).status_code == 401
    store.close()


def test_disallowed_host_and_origin_are_rejected() -> None:
    store = SQLiteStore.open(":memory:")
    queue = CommandQueue()
    with _client(_app(store, queue)) as client:
        # DNS-rebinding defence: an unexpected Host is refused.
        assert client.get("/runs", headers={"host": "attacker.example.com"}).status_code == 403
        # CSRF defence: a cross-origin request is refused.
        bad_origin = client.get("/runs", headers={"origin": "http://evil.example.com"})
        assert bad_origin.status_code == 403
    store.close()


def test_server_refuses_a_non_loopback_bind() -> None:
    store = SQLiteStore.open(":memory:")
    queue = CommandQueue()
    with pytest.raises(NonLoopbackBindError):
        serve(store=store, command_queue=queue, host="0.0.0.0", port=0)
    store.close()


# -- request validation ----------------------------------------------------------


def test_malformed_start_body_is_rejected() -> None:
    store = SQLiteStore.open(":memory:")
    queue = CommandQueue()
    with _client(_app(store, queue)) as client:
        assert client.post("/runs", json={}).status_code == 422  # missing 'workflow'
    store.close()


def test_unknown_run_returns_404() -> None:
    store = SQLiteStore.open(":memory:")
    queue = CommandQueue()
    with _client(_app(store, queue)) as client:
        assert client.get("/runs/nope/timeline").status_code == 404
        assert client.get("/runs/nope/tree").status_code == 404
    store.close()


# -- fork route exists and validates, deferring execution to V7 ------------------


async def test_fork_route_validates_and_defers_to_v7() -> None:
    store = SQLiteStore.open(":memory:")
    queue = CommandQueue()
    await start(demo.demo, 1, store=store, run_id="src").result()

    with _client(_app(store, queue)) as client:
        ok = client.post("/runs/src/fork", json={"fork_point_seq": 1})
        assert ok.status_code == 202
        assert ok.json()["deferred"] == "v7"

        # Malformed / invalid fork requests are rejected on the stable surface.
        assert client.post("/runs/src/fork", json={"fork_point_seq": 9999}).status_code == 400
        assert client.post("/runs/nope/fork", json={"fork_point_seq": 1}).status_code == 400
        assert client.post("/runs/src/fork", json={}).status_code == 422
    store.close()
