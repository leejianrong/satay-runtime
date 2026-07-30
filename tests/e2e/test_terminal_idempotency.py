"""E2E: the terminal event is appended at most once per run (ADR-0004 journal integrity).

A run's terminal event (``WorkflowCompleted`` / ``WorkflowFailed``) is committed
*before* the denormalised ``runs.status`` flips, so a crash in that window leaves a run
whose **journal is already terminal** but whose **status is not**. The resume path then
re-drove the workflow and appended a *second* terminal event: the outcome still read
correctly, but the journal carried a duplicate — an append-only journal must record one
terminal transition per run (ADR-0004). Driving a terminal journal is now an idempotent
no-op that only reconciles the status.

Driven through the primary seam (ADR-0011): the public ``satay.start`` API, an in-memory
``SQLiteStore``, and the ``FaultInjector`` crash-after-named-event hook. Only observable
outcomes are asserted — the journal, the run status, the result, and the demo
execution-count marker.
"""

from __future__ import annotations

import pytest

from satay import demo
from satay.api.decorators import task, workflow
from satay.api.primitives import start
from satay.api.run_handle import WorkflowFailedError
from satay.journal.events import EventType
from satay.journal.store import SQLiteStore
from satay.testing.faults import FaultInjector, SimulatedCrash

#: The event types that terminate a run — at most one may appear in a journal.
_TERMINAL_TYPES = (
    EventType.WORKFLOW_COMPLETED,
    EventType.WORKFLOW_FAILED,
    EventType.WORKFLOW_CANCELLED,
)


@pytest.fixture(autouse=True)
def _reset_marker() -> None:
    demo.reset_executions()


@task()
async def terminal_boom(value: int) -> int:
    """Always fails, marking a real execution first (proves it is not re-run on resume)."""
    demo.record_execution("terminal_boom")
    raise RuntimeError("terminal boom")


@workflow
async def terminal_boom_wf(value: int) -> int:
    """A one-task workflow that always records ``WorkflowFailed``."""
    return await terminal_boom(value)


async def _terminal_types(store: SQLiteStore, run_id: str) -> list[EventType]:
    events = await store.read_events(run_id)
    return [e.type for e in events if e.type in _TERMINAL_TYPES]


async def test_crash_after_workflow_completed_appends_no_second_terminal_event() -> None:
    """Crash between ``WorkflowCompleted`` and the status flip: resume must not duplicate it."""
    store = SQLiteStore.open(":memory:")
    injector = FaultInjector()
    injector.crash_after("WorkflowCompleted")  # dies after the terminal event commits

    handle = start(demo.demo, 1, store=store, injector=injector)
    with pytest.raises(SimulatedCrash):
        await handle.result()
    # The journal is terminal, but the crash beat the status flip → the run reads non-terminal.
    assert await _terminal_types(store, handle.run_id) == [EventType.WORKFLOW_COMPLETED]
    assert await handle.status() == "running"

    # Restart: resuming a journal that is already terminal is an idempotent no-op.
    resumed = start(demo.demo, 1, run_id=handle.run_id, store=store)
    assert await resumed.result() == 4
    assert await resumed.status() == "completed"

    # Exactly ONE terminal event — the run's single terminal transition (ADR-0004).
    assert await _terminal_types(store, handle.run_id) == [EventType.WORKFLOW_COMPLETED]
    # Both tasks had completed before the crash; neither re-executed on resume.
    assert demo.execution_count("step_one") == 1
    assert demo.execution_count("step_two") == 1
    store.close()


async def test_crash_after_workflow_failed_appends_no_second_terminal_event() -> None:
    """The failure mirror: a recorded ``WorkflowFailed`` is not re-appended on resume."""
    store = SQLiteStore.open(":memory:")
    injector = FaultInjector()
    injector.crash_after("WorkflowFailed")

    handle = start(terminal_boom_wf, 1, store=store, injector=injector)
    with pytest.raises(SimulatedCrash):
        await handle.result()
    assert await _terminal_types(store, handle.run_id) == [EventType.WORKFLOW_FAILED]
    assert await handle.status() == "running"
    assert demo.execution_count("terminal_boom") == 1

    resumed = start(terminal_boom_wf, 1, run_id=handle.run_id, store=store)
    with pytest.raises(WorkflowFailedError) as excinfo:
        await resumed.result()
    assert excinfo.value.error_type == "RuntimeError"
    assert await resumed.status() == "failed"

    assert await _terminal_types(store, handle.run_id) == [EventType.WORKFLOW_FAILED]
    # The recorded failure is replayed from the journal, not re-produced by re-running.
    assert demo.execution_count("terminal_boom") == 1
    store.close()


async def test_crash_after_child_workflow_completed_appends_no_second_terminal_event() -> None:
    """The same hole on a linked child run: the parent resume must not duplicate its terminal."""
    store = SQLiteStore.open(":memory:")
    injector = FaultInjector()
    injector.crash_after("WorkflowCompleted")  # the child completes first → crashes there

    handle = start(demo.parent_workflow, 2, store=store, injector=injector)
    with pytest.raises(SimulatedCrash):
        await handle.result()
    events = await store.read_events(handle.run_id)
    scheduled = next(e for e in events if e.type is EventType.CHILD_WORKFLOW_SCHEDULED)
    child_run_id = scheduled.payload["child_run_id"]
    assert await _terminal_types(store, child_run_id) == [EventType.WORKFLOW_COMPLETED]

    resumed = start(demo.parent_workflow, 2, run_id=handle.run_id, store=store)
    assert await resumed.result() == 21  # child 2*10, parent +1
    assert await resumed.status() == "completed"

    assert await _terminal_types(store, child_run_id) == [EventType.WORKFLOW_COMPLETED]
    assert await _terminal_types(store, handle.run_id) == [EventType.WORKFLOW_COMPLETED]
    assert demo.execution_count("child_task") == 1
    store.close()
