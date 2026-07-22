"""End-to-end acceptance tests for SLICE V1 — the crash-recovery headline.

Driven entirely through the primary seam (ADR-0011): the public ``satay.start`` API,
a temp/``:memory:`` ``SQLiteStore``, the ``FaultInjector`` crash hook, and the
``ManualClock``. Reuse-versus-execution is proven by the demo execution-count marker
and the journal — never by spying on the executor.
"""

from __future__ import annotations

import pytest

from satay import demo
from satay.api.primitives import start
from satay.api.run_handle import WorkflowFailedError
from satay.journal.events import EventType
from satay.journal.store import SQLiteStore
from satay.journal.timeline import interruption_seqs
from satay.testing.clock import ManualClock
from satay.testing.faults import FaultInjector, SimulatedCrash


@pytest.fixture(autouse=True)
def _reset_marker() -> None:
    demo.reset_executions()


def _types(events: object) -> list[EventType]:
    return [e.type for e in events]  # type: ignore[attr-defined]


async def test_run_creation_records_input_and_code_version() -> None:
    store = SQLiteStore.open(":memory:")
    handle = start(demo.demo, 3, store=store)
    assert isinstance(handle.run_id, str) and handle.run_id
    await handle.result()

    events = await store.read_events(handle.run_id)
    created = events[0]
    assert created.type is EventType.WORKFLOW_CREATED
    assert created.payload["workflow_name"] == "demo"
    assert created.payload["input_ref"] == 3
    assert created.payload["code_version"]  # stamped
    store.close()


async def test_every_transition_is_persisted_with_monotonic_seq() -> None:
    store = SQLiteStore.open(":memory:")
    handle = start(demo.demo, 1, store=store)
    await handle.result()
    events = list(await store.read_events(handle.run_id))
    assert [e.seq for e in events] == list(range(1, len(events) + 1))
    assert _types(events)[0] is EventType.WORKFLOW_CREATED
    assert _types(events)[-1] is EventType.WORKFLOW_COMPLETED
    store.close()


async def test_crash_after_task_completed_reuses_first_task() -> None:
    """The signature test: crash after step_one's TaskCompleted, resume, prove reuse."""
    store = SQLiteStore.open(":memory:")
    injector = FaultInjector()
    injector.crash_after("TaskCompleted")  # dies right after step_one commits

    handle = start(demo.demo, 1, store=store, injector=injector)
    with pytest.raises(SimulatedCrash):
        await handle.result()
    # step_one executed once before the crash; step_two never ran.
    assert demo.execution_count("step_one") == 1
    assert demo.execution_count("step_two") == 0

    # Restart: resume the same run_id (no fault armed now).
    resumed = start(demo.demo, 1, run_id=handle.run_id, store=store)
    result = await resumed.result()

    assert result == 4  # (1+1)=2, (2*2)=4
    # step_one was REUSED (still 1), step_two executed (now 1).
    assert demo.execution_count("step_one") == 1
    assert demo.execution_count("step_two") == 1

    events = list(await store.read_events(handle.run_id))
    # step_one has exactly one TaskCompleted (reused, not re-run).
    step_one_completions = [
        e
        for e in events
        if e.type is EventType.TASK_COMPLETED and e.payload["task_name"] == "step_one"
    ]
    assert len(step_one_completions) == 1
    # WorkflowResumed present → the ⚡ interruption marker.
    assert any(e.type is EventType.WORKFLOW_RESUMED for e in events)
    assert interruption_seqs(events)
    store.close()


async def test_crash_before_first_completed_reruns_task() -> None:
    """Crash after TaskScheduled (before any TaskCompleted): a miss that re-runs."""
    store = SQLiteStore.open(":memory:")
    injector = FaultInjector()
    injector.crash_after("TaskScheduled")  # dies before step_one's body runs

    handle = start(demo.demo, 1, store=store, injector=injector)
    with pytest.raises(SimulatedCrash):
        await handle.result()
    assert demo.execution_count("step_one") == 0  # crashed before the body ran

    resumed = start(demo.demo, 1, run_id=handle.run_id, store=store)
    result = await resumed.result()

    assert result == 4
    # The miss re-ran step_one (now executed) and step_two.
    assert demo.execution_count("step_one") == 1
    assert demo.execution_count("step_two") == 1
    store.close()


async def test_native_error_records_workflow_failed_with_traceback() -> None:
    from satay.api.decorators import task, workflow

    @task()
    async def e2e_explode(value: int) -> int:
        raise RuntimeError("native boom")

    @workflow
    async def e2e_bad(value: int) -> int:
        return await e2e_explode(value)

    store = SQLiteStore.open(":memory:")
    handle = start(e2e_bad, 1, store=store)
    with pytest.raises(WorkflowFailedError) as excinfo:
        await handle.result()
    assert excinfo.value.error_type == "RuntimeError"
    assert "native boom" in excinfo.value.error_message
    assert "Traceback" in excinfo.value.traceback_str

    events = await store.read_events(handle.run_id)
    assert events[-1].type is EventType.WORKFLOW_FAILED
    assert await handle.status() == "failed"
    store.close()


async def test_start_against_terminal_run_is_noop() -> None:
    store = SQLiteStore.open(":memory:")
    handle = start(demo.demo, 1, store=store)
    first = await handle.result()
    assert first == 4
    counts_before = (demo.execution_count("step_one"), demo.execution_count("step_two"))

    # Start again against the terminal run: no re-drive, no re-execution.
    again = start(demo.demo, 1, run_id=handle.run_id, store=store)
    second = await again.result()
    assert second == 4
    assert (demo.execution_count("step_one"), demo.execution_count("step_two")) == counts_before
    # No second WorkflowCompleted appended.
    events = await store.read_events(handle.run_id)
    assert _types(events).count(EventType.WORKFLOW_COMPLETED) == 1
    store.close()


async def test_clock_seam_stamps_event_timestamps() -> None:
    """The manual clock is wired through start → events carry its virtual time."""
    clock = ManualClock()
    store = SQLiteStore.open(":memory:")
    handle = start(demo.demo, 1, store=store, clock=clock)
    await handle.result()
    events = await store.read_events(handle.run_id)
    assert all(e.ts == clock.now() for e in events)  # virtual time never advanced
    store.close()
