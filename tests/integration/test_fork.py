"""Boundary tests for fork seeding + the version-mismatch read field (N15/N17, V7).

Pure, no FastAPI (ADR-0011 H3): drive the fork through the command queue + worker (the
single-writer path the HTTP route enqueues onto) and the read-view builders directly
against a temp ``SQLiteStore`` seeded through the V1 seam. The full acceptance flows
live in ``tests/e2e/test_fork_and_version.py``; these assert the discrete boundaries —
fork-point seeding + downstream re-run, source byte-for-byte immutability, the read
API's version-mismatch field, and the compare endpoint's per-call data the view diffs.
"""

from __future__ import annotations

import pytest

from satay import demo, versioning
from satay.api.primitives import start
from satay.control.api import ControlAPI
from satay.control.commands import CommandQueue
from satay.control.views import compare, run_list, timeline
from satay.journal.codec import decode
from satay.journal.events import EventType, RunStatus
from satay.journal.store import SQLiteStore
from satay.timers import TimerEventWorker


@pytest.fixture(autouse=True)
def _reset() -> None:
    demo.reset_executions()


def _step_one_completion_seq(events: object) -> int:
    return max(
        e.seq  # type: ignore[attr-defined]
        for e in events  # type: ignore[attr-defined]
        if e.type is EventType.TASK_COMPLETED and e.payload["task_name"] == "step_one"
    )


async def test_fork_seeds_to_fork_point_and_reruns_downstream_under_changed_task() -> None:
    store = SQLiteStore.open(":memory:")
    queue = CommandQueue()
    control = ControlAPI(store, queue)
    worker = TimerEventWorker(store=store, commands=queue)

    # Source run under the original task behaviour (bump = 1): step_one(1)=2, fork_step(2)=3.
    await start(demo.fork_demo, 1, store=store, run_id="src").result()
    assert demo.execution_count("step_one") == 1
    assert demo.execution_count("fork_step") == 1
    fork_point = _step_one_completion_seq(await store.read_events("src"))

    # Change the downstream task's behaviour, then fork from before it.
    demo.FORK_STEP_BUMP["amount"] = 100
    new_id = await control.fork("src", fork_point)
    await worker.tick()  # the worker seeds the fork's journal and drives it

    record = await store.get_run(new_id)
    assert record is not None and record.status is RunStatus.COMPLETED
    # step_one is a journal hit (reused, not re-run); fork_step re-ran under the change.
    assert demo.execution_count("step_one") == 1
    assert demo.execution_count("fork_step") == 2
    # The fork's downstream picked up the change: fork_step(2) = 2 + 100 = 102.
    fk_events = await store.read_events(new_id)
    completed = [e for e in fk_events if e.type is EventType.WORKFLOW_COMPLETED]
    assert decode(completed[-1].payload["output_ref"]) == 102
    store.close()


async def test_source_journal_is_byte_for_byte_unchanged_after_a_fork() -> None:
    store = SQLiteStore.open(":memory:")
    queue = CommandQueue()
    control = ControlAPI(store, queue)
    worker = TimerEventWorker(store=store, commands=queue)

    await start(demo.demo, 1, store=store, run_id="src").result()
    before = [
        (e.seq, e.event_id, e.type.value, dict(e.payload), e.ts.isoformat())
        for e in await store.read_events("src")
    ]

    new_id = await control.fork("src", 4)
    await worker.tick()

    after = [
        (e.seq, e.event_id, e.type.value, dict(e.payload), e.ts.isoformat())
        for e in await store.read_events("src")
    ]
    assert after == before  # the key property: history is never rewritten (ADR-0004)
    assert new_id != "src"
    assert any(e.type is EventType.RUN_FORKED for e in await store.read_events(new_id))
    store.close()


async def test_read_api_exposes_version_mismatch_field_on_affected_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SQLiteStore.open(":memory:")
    await start(demo.demo, 1, store=store, run_id="r1").result()
    record = await store.get_run("r1")
    assert record is not None

    # Matching current version → no mismatch (the additive field is always present).
    monkeypatch.setattr(versioning, "current_code_version", lambda: record.code_version)
    tl = await timeline(store, "r1")
    assert tl["version_mismatch"]["stamped"] == record.code_version
    assert tl["version_mismatch"]["mismatch"] is False

    # A changed current version → mismatch True on the affected run (the banner's source).
    monkeypatch.setattr(versioning, "current_code_version", lambda: "changed:v2")
    tl2 = await timeline(store, "r1")
    assert tl2["version_mismatch"]["mismatch"] is True
    assert tl2["version_mismatch"]["current"] == "changed:v2"
    # run_list carries the same field so the banner can render from the runs list too.
    listing = await run_list(store)
    assert listing["runs"][0]["version_mismatch"]["mismatch"] is True
    store.close()


async def test_compare_endpoint_returns_per_call_data_for_the_side_by_side_view() -> None:
    store = SQLiteStore.open(":memory:")
    await start(demo.demo, 1, store=store, run_id="a").result()
    await start(demo.demo, 5, store=store, run_id="b").result()

    result = await compare(store, "a", "b")
    row = next(r for r in result["rows"] if r["identity"] == "step_one:0")
    # Additive per-call fields the Studio view diffs on (inputs/outputs/attempts/timing).
    for field in ("task_name", "status", "input", "output", "attempts", "duration_seconds"):
        assert field in row["a"] and field in row["b"]
    assert row["a"]["input"] == [1] and row["a"]["output"] == 2  # step_one(1)
    assert row["b"]["input"] == [5] and row["b"]["output"] == 6  # step_one(5)
    store.close()
