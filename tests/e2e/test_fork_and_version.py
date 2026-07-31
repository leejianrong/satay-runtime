"""End-to-end acceptance tests for SLICE V7 — fork, lineage, version mismatch.

Driven through the primary seam (ADR-0011): the public ``satay.start`` API, a temp
``SQLiteStore``, the command queue + ``TimerEventWorker`` the HTTP fork route enqueues
onto, and the ``FaultInjector`` crash hook. Reuse-versus-re-execution is proven by the
demo execution-count marker and the journal, never by spying on internals. Per the test
plan the E2E twins (source-unchanged, ``RunForked`` lineage, mismatch policy) collapse
here alongside the boundary units in the integration tier.
"""

from __future__ import annotations

import logging

import pytest

from satay import demo, versioning
from satay.api.primitives import start
from satay.control.api import ControlAPI
from satay.control.commands import CommandQueue, ForkValidationError
from satay.control.views import timeline
from satay.journal.events import EventType, RunStatus
from satay.journal.store import SQLiteStore
from satay.testing.faults import FaultInjector, SimulatedCrash
from satay.timers import TimerEventWorker


@pytest.fixture(autouse=True)
def _reset() -> None:
    demo.reset_executions()


def _completion_seq(events: object, task_name: str) -> int:
    return max(
        e.seq  # type: ignore[attr-defined]
        for e in events  # type: ignore[attr-defined]
        if e.type is EventType.TASK_COMPLETED and e.payload["task_name"] == task_name
    )


async def test_fork_from_completed_run_reruns_downstream_source_untouched() -> None:
    """The headline: fork a completed run under changed code; original stays intact."""
    store = SQLiteStore.open(":memory:")
    queue = CommandQueue()
    control = ControlAPI(store, queue)
    worker = TimerEventWorker(store=store, commands=queue)

    # Source completes under bump=1: step_one(1)=2, fork_step(2)=3.
    src_result = await start(demo.fork_demo, 1, store=store, run_id="src").result()
    assert src_result == 3
    source_before = list(await store.read_events("src"))
    fork_point = _completion_seq(source_before, "step_one")

    # Change the downstream task, then fork from before it.
    demo.FORK_STEP_BUMP["amount"] = 100
    new_id = await control.fork("src", fork_point)
    await worker.tick()

    # The fork re-ran the downstream under the change: step_one reused, fork_step(2)=102.
    fork_result = await start(demo.fork_demo, 1, run_id=new_id, store=store).result()
    assert fork_result == 102
    assert demo.execution_count("step_one") == 1  # reused across the fork (journal hit)
    assert demo.execution_count("fork_step") == 2  # source once + fork once (re-run)

    # The source run is byte-for-byte unchanged and still returns its original result.
    assert list(await store.read_events("src")) == source_before
    assert await start(demo.fork_demo, 1, run_id="src", store=store).result() == 3
    store.close()


async def test_run_forked_lineage_chain_across_a_fork_of_a_fork() -> None:
    """RunForked records source + fork-point; a fork-of-a-fork yields a correct chain."""
    store = SQLiteStore.open(":memory:")
    queue = CommandQueue()
    control = ControlAPI(store, queue)
    worker = TimerEventWorker(store=store, commands=queue)

    await start(demo.demo, 1, store=store, run_id="A").result()
    a_point = _completion_seq(await store.read_events("A"), "step_one")

    b_id = await control.fork("A", a_point)
    await worker.tick()
    # Fork B again, from before its re-run of step_two (keeping through B's own RunForked).
    b_fork_event = next(e for e in await store.read_events(b_id) if e.type is EventType.RUN_FORKED)
    c_id = await control.fork(b_id, b_fork_event.seq)
    await worker.tick()

    # Lineage is traceable one hop at a time: C ← B ← A.
    b_line = (await timeline(store, b_id))["forked_from"]
    c_line = (await timeline(store, c_id))["forked_from"]
    assert b_line == {"source_run_id": "A", "fork_point_seq": a_point}
    assert c_line == {"source_run_id": b_id, "fork_point_seq": b_fork_event.seq}

    # C carries the ancestor's copied RunForked plus its own; the max-seq one is C's own.
    c_forked = [e for e in await store.read_events(c_id) if e.type is EventType.RUN_FORKED]
    assert len(c_forked) == 2
    assert max(c_forked, key=lambda e: e.seq).payload["source_run_id"] == b_id
    store.close()


async def test_forking_an_actively_executing_run_is_rejected_naming_status() -> None:
    """MVP forks terminal runs only; a live (waiting) run is rejected (Q53, live-run deferred)."""
    store = SQLiteStore.open(":memory:")
    queue = CommandQueue()
    control = ControlAPI(store, queue)

    # review_demo parks on wait_for_event → non-terminal (waiting).
    await start(demo.review_demo, 0, store=store, run_id="live").result()
    assert (await store.get_run("live")).status is RunStatus.WAITING

    with pytest.raises(ForkValidationError) as excinfo:
        await control.fork("live", 1)
    assert "waiting" in str(excinfo.value)  # the error names the run's status
    store.close()


async def test_version_mismatch_on_resume_rejected_in_strict_and_warns_in_dev(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    store = SQLiteStore.open(":memory:")
    injector = FaultInjector()
    injector.crash_after("TaskScheduled")  # crash mid-run → left RUNNING (non-terminal)

    handle = start(demo.demo, 1, store=store, injector=injector, run_id="v1")
    with pytest.raises(SimulatedCrash):
        await handle.result()
    assert (await store.get_run("v1")).status is RunStatus.RUNNING

    # Simulate resuming under a different code version.
    monkeypatch.setattr(versioning, "current_code_version", lambda: "changed:v2")

    # strict: the resume is rejected and the run is left untouched (no WorkflowResumed).
    with pytest.raises(versioning.VersionMismatchError):
        await start(demo.demo, 1, run_id="v1", store=store, version_mismatch="strict").result()
    events = await store.read_events("v1")
    assert not any(e.type is EventType.WORKFLOW_RESUMED for e in events)
    assert (await store.get_run("v1")).status is RunStatus.RUNNING

    # dev/warn: the same mismatch warns (offering a fork) but the resume proceeds.
    with caplog.at_level(logging.WARNING, logger="satay"):
        result = await start(
            demo.demo, 1, run_id="v1", store=store, version_mismatch="warn"
        ).result()
    assert result == 4  # the run completed on resume
    assert "mismatch" in caplog.text.lower()
    assert any(e.type is EventType.WORKFLOW_RESUMED for e in await store.read_events("v1"))
    store.close()
