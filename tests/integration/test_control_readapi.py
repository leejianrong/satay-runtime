"""Boundary tests for the V5 control/read layer — pure, no FastAPI (ADR-0011 H3).

These drive the read-view builders, the redactor, and the write-then-poll path directly
against a temp ``SQLiteStore`` seeded through the V1 seam. They import **no** studio
dependency, so they run under the plain dev env and stay collectable by the
import-hygiene guard. The HTTP-transport specifics (security headers, status codes,
malformed bodies, non-loopback refusal) live in ``tests/e2e/test_control_http.py``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from satay import demo
from satay.api.decorators import task, workflow
from satay.api.primitives import start
from satay.control.api import ControlAPI, ReadAPI
from satay.control.commands import CommandQueue, append_cancellation
from satay.control.redaction import REDACTED
from satay.control.views import RunNotFoundError, compare, run_list, task_detail, timeline, tree
from satay.journal.events import EventType, RunStatus, utc_now
from satay.journal.store import SQLiteStore
from satay.testing.clock import ManualClock
from satay.testing.faults import FaultInjector
from satay.timers import TimerEventWorker


@pytest.fixture(autouse=True)
def _reset() -> None:
    demo.reset_executions()


# -- fixtures for redaction ------------------------------------------------------


@dataclass(frozen=True)
class RxInput:
    api_key: str
    label: str


@task()
async def rx_task(payload: RxInput) -> dict[str, str]:
    return {"password": "leaked-value", "ok": payload.label}


@workflow
async def rx_wf(payload: RxInput) -> dict[str, str]:
    return await rx_task(payload)


# -- read views derive from a seeded journal -------------------------------------


async def test_run_list_and_timeline_derive_from_journal() -> None:
    store = SQLiteStore.open(":memory:")
    await start(demo.demo, 1, store=store, run_id="r1").result()

    listing = await run_list(store)
    assert [r["run_id"] for r in listing["runs"]] == ["r1"]
    row = listing["runs"][0]
    assert row["status"] == "completed"
    assert row["workflow_name"] == "demo"
    assert "code_version" in row and "created_at" in row

    tl = await timeline(store, "r1")
    types = [e["type"] for e in tl["events"]]
    assert types[0] == EventType.WORKFLOW_CREATED.value
    assert EventType.WORKFLOW_COMPLETED.value in types
    assert tl["interrupted"] is False
    # Events keep seq order and carry payload + interruption flag.
    assert tl["events"][0]["seq"] == 1
    assert all("payload" in e and "is_interruption" in e for e in tl["events"])
    store.close()


async def test_task_detail_groups_attempts_input_output_and_usage() -> None:
    store = SQLiteStore.open(":memory:")
    await start(demo.usage_demo, 1, store=store, run_id="u1").result()

    detail = await task_detail(store, "u1", "usage_task:0")
    assert detail["task_name"] == "usage_task"
    assert detail["status"] == "completed"
    assert detail["input"] == [1]  # single positional arg recorded on TaskScheduled
    assert detail["output"] == 2
    assert detail["usage"] == [{"model": "demo-model", "input_tokens": 10, "output_tokens": 5}]
    assert len(detail["attempts"]) == 1
    assert detail["attempts"][0]["status"] == "completed"
    assert detail["attempts"][0]["duration_seconds"] is not None
    store.close()


async def test_task_detail_records_retry_attempts_reason_and_delay() -> None:
    store = SQLiteStore.open(":memory:")
    clock = ManualClock()
    from satay.testing.rng import SeededRng

    # flaky_thrice fails twice then succeeds — three physical attempts on one identity.
    async def _run() -> object:
        return await start(
            demo.flaky_demo, 1, store=store, run_id="f1", clock=clock, rng=SeededRng(7)
        ).result()

    task_ = asyncio.ensure_future(_run())
    for _ in range(2000):
        for _ in range(4):
            await asyncio.sleep(0)
        if task_.done():
            break
        if clock.pending_sleepers:
            clock.advance(61.0)
    await task_

    detail = await task_detail(store, "f1", "flaky_thrice:0")
    assert [a["status"] for a in detail["attempts"]] == ["failed", "failed", "completed"]
    assert detail["attempts"][0]["error"]["type"] == "RuntimeError"
    assert detail["attempts"][0]["next_delay"] is not None  # retry delay recorded
    assert detail["attempts"][2]["status"] == "completed"  # last attempt succeeded
    assert detail["status"] == "completed"
    store.close()


async def test_tree_reconstructs_map_items() -> None:
    store = SQLiteStore.open(":memory:")
    await start(demo.map_square_demo, [1, 2, 3], store=store, run_id="m1").result()

    result = await tree(store, "m1")
    map_nodes = [n for n in result["nodes"] if n["kind"] == "map"]
    assert len(map_nodes) == 1
    node = map_nodes[0]
    assert node["task_name"] == "square_item"
    assert sorted(item["key"] for item in node["items"]) == ["item-1", "item-2", "item-3"]
    assert node["status"] == "completed"
    store.close()


async def test_tree_reconstructs_parent_child_linkage() -> None:
    store = SQLiteStore.open(":memory:")
    await start(demo.parent_workflow, 2, store=store, run_id="p1").result()

    result = await tree(store, "p1")
    child_nodes = [n for n in result["nodes"] if n["kind"] == "child"]
    assert len(child_nodes) == 1
    child = child_nodes[0]
    assert child["workflow_name"] == "child_workflow"
    assert child["status"] == "completed"
    # The child's own tree is nested in (V4 linkage recovered both ways).
    assert child["tree"]["run_id"] == child["child_run_id"]
    assert any(n["task_name"] == "child_task" for n in child["tree"]["nodes"])
    store.close()


async def test_compare_derives_two_aligned_runs() -> None:
    store = SQLiteStore.open(":memory:")
    await start(demo.demo, 1, store=store, run_id="c1").result()
    await start(demo.demo, 5, store=store, run_id="c2").result()

    result = await compare(store, "c1", "c2")
    identities = {row["identity"] for row in result["rows"]}
    assert identities == {"step_one:0", "step_two:0"}
    assert all(row["aligned"] for row in result["rows"])
    store.close()


async def test_unknown_run_raises_not_found() -> None:
    store = SQLiteStore.open(":memory:")
    for coro in (timeline(store, "nope"), tree(store, "nope")):
        with pytest.raises(RunNotFoundError):
            await coro
    store.close()


# -- redaction: no unredacted read path (N18) ------------------------------------


async def test_redaction_strips_secrets_on_every_read_endpoint() -> None:
    import json

    store = SQLiteStore.open(":memory:")
    await start(
        rx_wf, RxInput(api_key="super-secret-123", label="hello"), store=store, run_id="s1"
    ).result()

    reads = ReadAPI(store)
    for coro in (
        reads.timeline("s1"),
        reads.task_detail("s1", "rx_task:0"),
        reads.compare("s1", "s1"),
    ):
        blob = json.dumps(await coro)
        assert "super-secret-123" not in blob  # input api_key redacted
        assert "leaked-value" not in blob  # output password redacted
        assert REDACTED in blob

    # The raw builder (below the redactor) still contains the secret — proving redaction
    # is the read-time transform, not a storage-time one (there is no unredacted READ).
    raw = json.dumps(await timeline(store, "s1"))
    assert "super-secret-123" in raw
    store.close()


# -- write-then-poll: writes route through the worker (single writer, ADR-0012) --


async def test_http_start_is_applied_by_the_worker_poll_loop() -> None:
    store = SQLiteStore.open(":memory:")
    queue = CommandQueue()
    control = ControlAPI(store, queue)
    worker = TimerEventWorker(store=store, commands=queue)

    run_id = control.start("demo", 1)  # enqueue only; nothing driven yet
    assert await store.get_run(run_id) is None  # not written until the worker ticks

    await worker.tick()  # the worker drains the queue and drives the run
    record = await store.get_run(run_id)
    assert record is not None and record.status is RunStatus.COMPLETED
    assert demo.execution_count("step_one") == 1
    store.close()


async def test_http_send_event_lands_in_inbox_and_resumes_waiting_run() -> None:
    clock = ManualClock()
    store = SQLiteStore.open(":memory:")
    queue = CommandQueue()
    control = ControlAPI(store, queue)
    worker = TimerEventWorker(store=store, clock=clock, commands=queue)

    # Start a run that parks on wait_for_event, applied through the worker.
    run_id = control.start("review_demo", 0)
    await worker.tick()
    assert (await store.get_run(run_id)).status is RunStatus.WAITING

    # Deliver the event over the "HTTP" path: one delivery path with the Python API.
    control.send_event(
        "satay.demo.ReviewDecision",
        key=demo.REVIEW_KEY,
        payload={"approved": True, "reviewer": "alice"},
    )
    resumed = await worker.tick()  # applies SendEvent, then delivers on the same tick
    assert resumed == 1
    assert (await store.get_run(run_id)).status is RunStatus.COMPLETED
    events = await store.read_events(run_id)
    assert EventType.EXTERNAL_EVENT_RECEIVED in [e.type for e in events]
    store.close()


async def test_http_cancel_appends_workflow_cancelled_within_one_tick() -> None:
    clock = ManualClock()
    store = SQLiteStore.open(":memory:")
    queue = CommandQueue()
    control = ControlAPI(store, queue)
    worker = TimerEventWorker(store=store, clock=clock, commands=queue)

    run_id = control.start("review_demo", 0)
    await worker.tick()  # parks on the event wait
    assert (await store.get_run(run_id)).status is RunStatus.WAITING

    control.cancel(run_id)
    await worker.tick()  # cancel takes effect within one poll interval
    record = await store.get_run(run_id)
    assert record.status is RunStatus.CANCELLED
    assert EventType.WORKFLOW_CANCELLED in [e.type for e in await store.read_events(run_id)]
    store.close()


async def test_cancelled_run_settles_cleanly_and_is_not_resumed_by_a_later_event() -> None:
    """A run cancelled mid-wait halts: a subsequently delivered event never resumes it."""
    clock = ManualClock()
    store = SQLiteStore.open(":memory:")
    queue = CommandQueue()
    control = ControlAPI(store, queue)
    worker = TimerEventWorker(store=store, clock=clock, commands=queue)

    run_id = control.start("review_demo", 0)
    await worker.tick()
    control.cancel(run_id)
    await worker.tick()
    assert (await store.get_run(run_id)).status is RunStatus.CANCELLED

    # Deliver a matching event afterwards: the cancelled (terminal) run is never re-driven.
    control.send_event("satay.demo.ReviewDecision", key=demo.REVIEW_KEY, payload={"approved": True})
    resumed = await worker.tick()
    assert resumed == 0
    assert (await store.get_run(run_id)).status is RunStatus.CANCELLED
    # The buffered event is left unconsumed (never delivered to a halted run).
    assert len(await store.list_inbox_events(include_consumed=False)) == 1
    store.close()


async def test_handle_cancel_and_command_cancel_reach_the_same_transition() -> None:
    store = SQLiteStore.open(":memory:")

    # Path A: RunHandle.cancel() (N4) on a parked run.
    handle = start(demo.review_demo, 0, store=store, run_id="hc")
    await handle.result()  # parks
    await handle.cancel()
    a_events = [e.type for e in await store.read_events("hc")]

    # Path B: the command-queue cancel applied by the worker.
    queue = CommandQueue()
    control = ControlAPI(store, queue)
    worker = TimerEventWorker(store=store, commands=queue)
    control.start("review_demo", 0, run_id="cc")
    await worker.tick()  # parks
    control.cancel("cc")
    await worker.tick()
    b_events = [e.type for e in await store.read_events("cc")]

    assert EventType.WORKFLOW_CANCELLED in a_events
    assert EventType.WORKFLOW_CANCELLED in b_events
    assert (await store.get_run("hc")).status is RunStatus.CANCELLED
    assert (await store.get_run("cc")).status is RunStatus.CANCELLED
    store.close()


async def test_cancel_is_a_noop_on_a_terminal_run() -> None:
    store = SQLiteStore.open(":memory:")
    await start(demo.demo, 1, store=store, run_id="done").result()
    applied = await append_cancellation(store, "done", now=utc_now())
    assert applied is False
    assert (await store.get_run("done")).status is RunStatus.COMPLETED
    store.close()


# -- ADR-0012: reads never block behind a stalled worker -------------------------


async def test_reads_return_promptly_while_the_worker_is_stalled_mid_write() -> None:
    """The non-blocking-reads guarantee (ADR-0012): a stalled writer never blocks reads."""
    store = SQLiteStore.open(":memory:")
    injector = FaultInjector()
    gate = injector.stall_after(EventType.TASK_COMPLETED.value)  # stall post-commit, mid-run

    handle = start(demo.demo, 1, store=store, run_id="stall", injector=injector)
    drive = asyncio.ensure_future(handle.result())

    # Let the worker reach the stall point (step_one's TaskCompleted committed, then blocked).
    for _ in range(2000):
        await asyncio.sleep(0)
        events = await store.read_events("stall")
        if any(e.type is EventType.TASK_COMPLETED for e in events):
            break
    assert not drive.done()  # the sole writer is parked mid-write

    # A read still returns promptly on the same loop while the writer is stalled.
    reads = ReadAPI(store)
    listing = await asyncio.wait_for(reads.run_list(), timeout=2.0)
    assert [r["run_id"] for r in listing["runs"]] == ["stall"]
    tl = await asyncio.wait_for(reads.timeline("stall"), timeout=2.0)
    assert EventType.TASK_COMPLETED.value in [e["type"] for e in tl["events"]]

    gate.set()  # release the stall; the run finishes normally
    assert await asyncio.wait_for(drive, timeout=2.0) == 4
    store.close()
