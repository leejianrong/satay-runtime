"""End-to-end acceptance tests for SLICE V4 — composite primitives + parallel recovery.

Driven through the primary seam (ADR-0011): public ``satay.start``, a ``:memory:``
``SQLiteStore``, and the ``FaultInjector`` crash hook. Reuse-vs-re-execution is proven
by the per-key demo execution markers and the journal, never by spying on internals.
The composite failure paths follow the fail-fast semantics of ADR-0020.
"""

from __future__ import annotations

import pytest

from satay import demo
from satay.api.primitives import DEFAULT_MAP_CONCURRENCY, start
from satay.api.run_handle import WorkflowFailedError
from satay.journal.events import EventType
from satay.journal.store import SQLiteStore
from satay.testing.faults import FaultInjector, SimulatedCrash


@pytest.fixture(autouse=True)
def _reset_marker() -> None:
    demo.reset_executions()


def _completed_keys(events: object) -> list[str]:
    """The map key of every recorded ``TaskCompleted`` (keyed items only)."""
    return [
        e.payload["key"]  # type: ignore[attr-defined]
        for e in events  # type: ignore[attr-defined]
        if e.type is EventType.TASK_COMPLETED and "key" in e.payload  # type: ignore[attr-defined]
    ]


# -- map: identity by key, order-independence ------------------------------------


async def test_map_matches_each_result_to_its_key_under_parallel_completion() -> None:
    """Items rejoin by ``key=`` with no cross-contamination even when run in parallel."""
    store = SQLiteStore.open(":memory:")
    values = [2, 5, 3, 7, 4]
    handle = start(demo.bounded_map_demo, values, store=store)
    result = await handle.result()
    # gauge_item returns value+1; rejoined strictly in INPUT order regardless of timing.
    assert result == [v + 1 for v in values]
    store.close()


# -- the signature demo: partial-completion recovery -----------------------------


async def test_partial_completion_survives_crash_only_unresolved_reruns() -> None:
    """THE signature demo: crash mid-fan-out; on restart only unresolved items re-run."""
    store = SQLiteStore.open(":memory:")
    injector = FaultInjector()
    injector.crash_after("TaskCompleted")  # die right after the first item is recorded

    values = [1, 2, 3, 4]
    handle = start(demo.map_square_demo, values, store=store, injector=injector)
    with pytest.raises(SimulatedCrash):
        await handle.result()

    # Exactly one item was durably recorded before the crash; the rest never ran
    # (concurrency=1 makes the crash point deterministic).
    events = list(await store.read_events(handle.run_id))
    done_before = _completed_keys(events)
    assert len(done_before) == 1
    survived_key = done_before[0]
    assert demo.execution_count(survived_key) == 1
    assert sum(demo.execution_count(f"item-{v}") for v in values) == 1  # only the one ran

    # Restart: resume the same run (no fault). Completed item reused, others re-run.
    resumed = start(demo.map_square_demo, values, run_id=handle.run_id, store=store)
    result = await resumed.result()
    assert result == [1, 4, 9, 16]

    # The survivor was REUSED (still exactly one execution), never re-run.
    assert demo.execution_count(survived_key) == 1
    # Every item is complete now, each recorded exactly once (matched by key).
    final_events = list(await store.read_events(handle.run_id))
    assert sorted(_completed_keys(final_events)) == ["item-1", "item-2", "item-3", "item-4"]
    # ⚡ interruption marker present.
    assert any(e.type is EventType.WORKFLOW_RESUMED for e in final_events)
    store.close()


# -- gather: positional rejoin ---------------------------------------------------


async def test_gather_rejoins_heterogeneous_calls_positionally() -> None:
    """A scalar task and a nested map gather together, results in argument order."""
    store = SQLiteStore.open(":memory:")
    handle = start(demo.gather_demo, 5, store=store)
    result = await handle.result()
    assert result == [105, [1, 4, 9]]  # add_hundred(5)=105 ; squares of [1,2,3]
    assert demo.execution_count("add_hundred") == 1
    store.close()


# -- concurrency bound -----------------------------------------------------------


async def test_concurrency_bounds_in_flight_items() -> None:
    """``concurrency=N`` bounds in-flight items to N within the single process."""
    store = SQLiteStore.open(":memory:")
    handle = start(demo.bounded_map_demo, list(range(6)), store=store)
    await handle.result()
    assert demo.CONCURRENCY_GAUGE["peak"] == 2  # never more than 2 items in flight
    store.close()


async def test_unspecified_concurrency_uses_the_default_bound() -> None:
    """An unspecified ``concurrency=`` bounds in-flight items to the default."""
    store = SQLiteStore.open(":memory:")
    handle = start(demo.default_bound_map_demo, list(range(12)), store=store)
    await handle.result()
    assert demo.CONCURRENCY_GAUGE["peak"] == DEFAULT_MAP_CONCURRENCY
    store.close()


# -- fail-fast (ADR-0020) --------------------------------------------------------


async def test_failed_map_item_raises_through_the_map() -> None:
    """A failed item raises through the map (fail-fast); siblings' results are discarded."""
    store = SQLiteStore.open(":memory:")
    handle = start(demo.failing_map_demo, [1, 2, 3], store=store)
    with pytest.raises(WorkflowFailedError) as excinfo:
        await handle.result()
    assert "map item 2 boom" in excinfo.value.error_message
    assert await handle.status() == "failed"
    store.close()


async def test_failed_gather_member_fails_whole_gather() -> None:
    """A failed gather member fails the whole gather (fail-fast, ADR-0020)."""
    from satay.api.decorators import task, workflow

    @task()
    async def ok_member(value: int) -> int:
        return value

    @task()
    async def bad_member(value: int) -> int:
        raise RuntimeError("gather member boom")

    @workflow
    async def gather_fail_wf(value: int) -> list[int]:
        from satay.api.primitives import gather

        return await gather(ok_member(value), bad_member(value))

    store = SQLiteStore.open(":memory:")
    handle = start(gather_fail_wf, 1, store=store)
    with pytest.raises(WorkflowFailedError) as excinfo:
        await handle.result()
    assert "gather member boom" in excinfo.value.error_message
    store.close()


# -- child workflows -------------------------------------------------------------


async def test_child_run_is_linked_to_parent_and_reused() -> None:
    """A child run links to its parent both ways; a completed child is reused (no re-run)."""
    store = SQLiteStore.open(":memory:")
    handle = start(demo.parent_workflow, 5, store=store)
    result = await handle.result()
    assert result == 51  # child: 5*10=50 ; parent: +1

    parent_events = list(await store.read_events(handle.run_id))
    scheduled = next(e for e in parent_events if e.type is EventType.CHILD_WORKFLOW_SCHEDULED)
    child_run_id = scheduled.payload["child_run_id"]
    assert scheduled.payload["workflow_name"] == "child_workflow"

    # Reverse linkage on the child's WorkflowCreated (the V6 tree reads both directions).
    child_events = list(await store.read_events(child_run_id))
    child_created = child_events[0]
    assert child_created.type is EventType.WORKFLOW_CREATED
    assert child_created.payload["parent_run_id"] == handle.run_id
    assert child_created.payload["parent_call"]["task_name"] == "child:child_workflow"
    assert demo.execution_count("child_task") == 1

    # Resuming the terminal parent is a no-op: the completed child is reused, not re-run.
    again = start(demo.parent_workflow, 5, run_id=handle.run_id, store=store)
    assert await again.result() == 51
    assert demo.execution_count("child_task") == 1
    store.close()


async def test_failed_child_surfaces_and_reraises_deterministically() -> None:
    """A failed child surfaces as a raised exception, re-raised identically on replay."""
    store = SQLiteStore.open(":memory:")
    handle = start(demo.parent_of_failing_child, 5, store=store)
    with pytest.raises(WorkflowFailedError) as excinfo:
        await handle.result()
    assert "child workflow boom" in excinfo.value.error_message
    assert demo.execution_count("child_boom") == 1

    # Replay the terminal parent: the failure re-raises from the journal, child not re-run.
    again = start(demo.parent_of_failing_child, 5, run_id=handle.run_id, store=store)
    with pytest.raises(WorkflowFailedError):
        await again.result()
    assert demo.execution_count("child_boom") == 1  # deterministic — no re-execution
    store.close()


async def test_crashed_child_resumes_on_parent_resume() -> None:
    """A child crashed mid-flight is re-awaited on parent resume and resumes (not restarts)."""
    store = SQLiteStore.open(":memory:")
    injector = FaultInjector()
    injector.crash_after("TaskCompleted")  # die after the child's FIRST task is recorded

    handle = start(demo.parent_of_two_step_child, 5, store=store, injector=injector)
    with pytest.raises(SimulatedCrash):
        await handle.result()
    assert demo.execution_count("child_step_a") == 1  # first child task ran + recorded
    assert demo.execution_count("child_step_b") == 0  # crashed before the second

    # Restart the parent: the child RESUMES (reuses step_a) rather than restarting.
    resumed = start(demo.parent_of_two_step_child, 5, run_id=handle.run_id, store=store)
    result = await resumed.result()
    assert result == 12  # (5+1)*2

    assert demo.execution_count("child_step_a") == 1  # reused — the child was not restarted
    assert demo.execution_count("child_step_b") == 1  # only the unresolved step ran

    # The child journal carries its own resume marker (⚡), proving mid-flight resume.
    scheduled = next(
        e
        for e in await store.read_events(handle.run_id)
        if e.type is EventType.CHILD_WORKFLOW_SCHEDULED
    )
    child_events = list(await store.read_events(scheduled.payload["child_run_id"]))
    assert any(e.type is EventType.WORKFLOW_RESUMED for e in child_events)
    store.close()
