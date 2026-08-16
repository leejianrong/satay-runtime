"""End-to-end acceptance tests for collect-mode fan-out (KAN-473, ADR-0027).

Driven through the primary seam (ADR-0011): public ``satay.start``, a real ``SQLiteStore``,
the ``ManualClock``/``SeededRng`` determinism controls, and the ``FaultInjector`` crash
hook. Every assertion is on an observable outcome — the returned list, the run status, the
journal, or a per-task execution-count marker — never on replay internals.

The three claims under test:

1. a collected failure does not take its siblings' completed work down with it,
2. the failure stays **visible to the runtime** — ``TaskAttemptFailed`` per attempt plus a
   terminal ``TaskFailed`` — rather than becoming application data (the Cost 2 trap),
3. fail-fast is untouched unless you ask for collect mode.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

import satay
from satay.journal.events import EventType
from satay.journal.store import SQLiteStore
from satay.testing.clock import ManualClock
from satay.testing.faults import FaultInjector, SimulatedCrash

#: Per-task-key execution marker: proves reuse vs. re-execution across drives.
EXECUTIONS: Counter[str] = Counter()


@pytest.fixture(autouse=True)
def _reset_executions() -> None:
    EXECUTIONS.clear()


@satay.task()
async def collect_item(value: int) -> int:
    """Double even values; odd values are the poison item."""
    EXECUTIONS[f"item-{value}"] += 1
    if value % 2:
        raise ValueError(f"item {value} is poison")
    return value * 2


@satay.workflow
async def collect_map_wf(values: list[int]) -> list[str]:
    outcomes = await satay.map(
        collect_item, values, key=lambda v: f"item-{v}", concurrency=2, return_exceptions=True
    )
    # Rendered to strings so the workflow's own *result* is plain journalable data:
    # the exception objects never cross the codec.
    return [f"ERR:{o.error_type}" if isinstance(o, Exception) else f"OK:{o}" for o in outcomes]


@satay.workflow
async def failfast_map_wf(values: list[int]) -> list[int]:
    return await satay.map(collect_item, values, key=lambda v: f"item-{v}", concurrency=2)


def _events_of(events: object, event_type: EventType) -> list[object]:
    return [e for e in events if e.type is event_type]  # type: ignore[attr-defined,union-attr]


def _keys_of(events: object, event_type: EventType) -> list[str]:
    return [
        e.payload["key"]  # type: ignore[attr-defined]
        for e in events  # type: ignore[attr-defined]
        if e.type is event_type and "key" in e.payload  # type: ignore[attr-defined]
    ]


# -- 1. collect mode returns the siblings alongside the failure --------------------


async def test_collect_map_returns_siblings_beside_the_failure(temp_db_path: Path) -> None:
    """The whole point of KAN-473: paid-for sibling work comes back, in input order."""
    store = SQLiteStore.open(temp_db_path)
    handle = satay.start(collect_map_wf, [2, 3, 4, 5, 6], store=store)

    assert await handle.result() == ["OK:4", "ERR:ValueError", "OK:8", "ERR:ValueError", "OK:12"]
    assert await handle.status() == "completed"  # the run SURVIVES the item failures
    store.close()


async def test_collected_failure_is_a_task_failed_error_carrying_the_identity() -> None:
    """The slot holds ``satay.TaskFailedError``, naming the task, the key, and the cause."""
    captured: list[object] = []

    @satay.workflow
    async def capture_wf(values: list[int]) -> int:
        outcomes = await satay.map(
            collect_item, values, key=lambda v: f"item-{v}", return_exceptions=True
        )
        captured.extend(outcomes)
        return len(outcomes)

    store = SQLiteStore.open(":memory:")
    assert await satay.start(capture_wf, [2, 3], store=store).result() == 2

    ok, failure = captured
    assert ok == 4
    assert isinstance(failure, satay.TaskFailedError)
    assert isinstance(failure, RuntimeError)  # catchable next to WorkflowFailedError
    assert failure.task_name == "collect_item"
    assert failure.key == "item-3"
    assert failure.error_type == "ValueError"  # the class *name*, not an import path
    assert "item 3 is poison" in failure.error_message
    assert "ValueError" in failure.traceback_str
    # The original exception is still reachable on the pass that raised it.
    assert isinstance(failure.__cause__, ValueError)
    store.close()


# -- 2. the failure stays visible to the runtime -----------------------------------


async def test_collected_failure_is_recorded_in_the_journal(temp_db_path: Path) -> None:
    """Cost 2 of the card: the failure must be a journal fact, not application data."""
    store = SQLiteStore.open(temp_db_path)
    handle = satay.start(collect_map_wf, [2, 3, 4], store=store)
    await handle.result()

    events = list(await store.read_events(handle.run_id))
    assert _keys_of(events, EventType.TASK_COMPLETED) == ["item-2", "item-4"]
    assert _keys_of(events, EventType.TASK_FAILED) == ["item-3"]
    # The attempt that failed is recorded too, so retry policy and alerting can see it.
    assert _keys_of(events, EventType.TASK_ATTEMPT_FAILED) == ["item-3"]

    failed = _events_of(events, EventType.TASK_FAILED)[0]
    assert failed.payload["task_name"] == "collect_item"  # type: ignore[attr-defined]
    assert failed.payload["error"]["type"] == "ValueError"  # type: ignore[attr-defined]
    assert "item 3 is poison" in failed.payload["error"]["message"]  # type: ignore[attr-defined]

    # The run itself completed: no WorkflowFailed, and a green run is *not* what hides
    # the failure — the TaskFailed above is the record that keeps it honest.
    assert _events_of(events, EventType.WORKFLOW_FAILED) == []
    assert _events_of(events, EventType.WORKFLOW_COMPLETED) != []
    store.close()


async def test_collected_item_still_exhausts_its_retry_budget(
    temp_db_path: Path, manual_clock: ManualClock, drain: object
) -> None:
    """Retries are unchanged by collect mode: N+1 attempts, then one terminal TaskFailed."""

    @satay.task(retries=2)
    async def flaky(value: int) -> int:
        EXECUTIONS[f"flaky-{value}"] += 1
        raise RuntimeError("always down")

    @satay.workflow
    async def retry_collect_wf(values: list[int]) -> int:
        outcomes = await satay.map(flaky, values, key=lambda v: f"k{v}", return_exceptions=True)
        return sum(1 for o in outcomes if isinstance(o, satay.TaskFailedError))

    store = SQLiteStore.open(temp_db_path)
    handle = satay.start(retry_collect_wf, [1], store=store, clock=manual_clock)
    assert await drain(handle.result, manual_clock) == 1  # type: ignore[operator]

    assert EXECUTIONS["flaky-1"] == 3  # 1 initial + 2 retries
    events = list(await store.read_events(handle.run_id))
    assert len(_events_of(events, EventType.TASK_ATTEMPT_FAILED)) == 3
    assert len(_events_of(events, EventType.TASK_FAILED)) == 1  # exactly one terminal record
    store.close()


# -- 3. replay: a recorded failure is a hit, not a re-run --------------------------


async def test_partial_completion_recovery_still_works_mid_collect_fanout(
    temp_db_path: Path,
) -> None:
    """Crash mid-fan-out, resume: completed items are reused, only unresolved ones re-run."""
    store = SQLiteStore.open(temp_db_path)
    injector = FaultInjector()
    injector.crash_after("TaskCompleted")  # die right after the first item is recorded

    values = [2, 3, 4, 6]
    handle = satay.start(collect_map_wf, values, store=store, injector=injector)
    with pytest.raises(SimulatedCrash):
        await handle.result()

    before = list(await store.read_events(handle.run_id))
    survivors = _keys_of(before, EventType.TASK_COMPLETED)
    assert len(survivors) == 1
    executions_before = dict(EXECUTIONS)

    # Resume the same run with no fault armed.
    resumed = satay.start(collect_map_wf, values, run_id=handle.run_id, store=store)
    assert await resumed.result() == ["OK:4", "ERR:ValueError", "OK:8", "OK:12"]
    assert await resumed.status() == "completed"

    # The survivor was reused, not re-executed.
    survivor_value = survivors[0].removeprefix("item-")
    assert EXECUTIONS[f"item-{survivor_value}"] == executions_before[f"item-{survivor_value}"] == 1
    # Every item is recorded exactly once across both drives.
    after = list(await store.read_events(handle.run_id))
    assert sorted(_keys_of(after, EventType.TASK_COMPLETED)) == ["item-2", "item-4", "item-6"]
    assert _keys_of(after, EventType.TASK_FAILED) == ["item-3"]
    store.close()


async def test_recorded_failure_is_reused_on_resume_not_re_executed(
    temp_db_path: Path,
) -> None:
    """A recorded ``TaskFailed`` is a replay hit: the poison item is not paid for twice."""
    store = SQLiteStore.open(temp_db_path)
    injector = FaultInjector()
    injector.crash_after("TaskFailed")  # die the instant the failure becomes durable

    values = [3, 2]  # poison first, so the failure is recorded before the crash
    handle = satay.start(collect_map_wf, values, store=store, injector=injector)
    with pytest.raises(SimulatedCrash):
        await handle.result()
    assert EXECUTIONS["item-3"] == 1

    resumed = satay.start(collect_map_wf, values, run_id=handle.run_id, store=store)
    assert await resumed.result() == ["ERR:ValueError", "OK:4"]

    assert EXECUTIONS["item-3"] == 1  # NOT re-run — the failure replayed from the journal
    after = list(await store.read_events(handle.run_id))
    assert _keys_of(after, EventType.TASK_FAILED) == ["item-3"]  # recorded exactly once
    assert len(_events_of(after, EventType.TASK_ATTEMPT_FAILED)) == 1
    store.close()


# -- 4. fail-fast is unchanged by default ------------------------------------------


async def test_default_map_is_still_fail_fast_and_records_no_task_failed(
    temp_db_path: Path,
) -> None:
    """Without ``return_exceptions=`` nothing changes: the run fails, no TaskFailed event."""
    store = SQLiteStore.open(temp_db_path)
    handle = satay.start(failfast_map_wf, [2, 3, 4], store=store)
    with pytest.raises(satay.WorkflowFailedError) as excinfo:
        await handle.result()
    assert excinfo.value.error_type == "ValueError"
    assert await handle.status() == "failed"

    events = list(await store.read_events(handle.run_id))
    assert _events_of(events, EventType.TASK_FAILED) == []  # fail-fast journals are untouched
    assert _events_of(events, EventType.WORKFLOW_FAILED) != []
    store.close()


async def test_default_gather_is_still_fail_fast() -> None:
    """``gather`` keeps native-await semantics unless collect mode is asked for."""

    @satay.workflow
    async def gather_failfast_wf(value: int) -> list[int]:
        return await satay.gather(collect_item(2), collect_item(3))

    store = SQLiteStore.open(":memory:")
    with pytest.raises(satay.WorkflowFailedError):
        await satay.start(gather_failfast_wf, 0, store=store).result()
    store.close()


# -- 5. gather collect mode ---------------------------------------------------------


async def test_collect_gather_returns_members_positionally(temp_db_path: Path) -> None:
    """``gather(..., return_exceptions=True)`` collects per member, in argument order."""

    @satay.workflow
    async def gather_collect_wf(value: int) -> list[str]:
        outcomes = await satay.gather(
            collect_item(2), collect_item(3), collect_item(4), return_exceptions=True
        )
        return [f"ERR:{o.error_type}" if isinstance(o, Exception) else f"OK:{o}" for o in outcomes]

    store = SQLiteStore.open(temp_db_path)
    handle = satay.start(gather_collect_wf, 0, store=store)
    assert await handle.result() == ["OK:4", "ERR:ValueError", "OK:8"]
    assert await handle.status() == "completed"

    events = list(await store.read_events(handle.run_id))
    failed = _events_of(events, EventType.TASK_FAILED)
    assert len(failed) == 1
    # An ordinary (non-keyed) durable call records its ordinal, not a map key.
    assert failed[0].payload["ordinal"] == 1  # type: ignore[attr-defined]
    store.close()


async def test_collect_gather_collects_a_failed_child_run() -> None:
    """A failed child surfaces as ``WorkflowFailedError`` in its slot, already durable."""

    @satay.workflow
    async def doomed_child(value: int) -> int:
        return await collect_item(3)

    @satay.workflow
    async def parent_wf(value: int) -> list[str]:
        outcomes = await satay.gather(
            collect_item(2), satay.start_child(doomed_child, 0), return_exceptions=True
        )
        return [type(o).__name__ if isinstance(o, Exception) else f"OK:{o}" for o in outcomes]

    store = SQLiteStore.open(":memory:")
    handle = satay.start(parent_wf, 0, store=store)
    assert await handle.result() == ["OK:4", "WorkflowFailedError"]
    assert await handle.status() == "completed"
    store.close()


async def test_fail_fast_map_nested_in_a_collect_gather_stays_fail_fast() -> None:
    """An inner default ``map`` still *raises*; the outer collect gather catches it.

    And because the enclosing gather makes that failure survivable, the inner item's
    failure is still recorded as a terminal ``TaskFailed`` (the monotone rule, ADR-0027).
    """

    @satay.workflow
    async def nested_wf(value: int) -> list[str]:
        outcomes = await satay.gather(
            collect_item(2),
            satay.map(collect_item, [4, 5], key=lambda v: f"n{v}"),
            return_exceptions=True,
        )
        return [type(o).__name__ if isinstance(o, Exception) else str(o) for o in outcomes]

    store = SQLiteStore.open(":memory:")
    handle = satay.start(nested_wf, 0, store=store)
    outcomes = await handle.result()
    assert outcomes == ["4", "TaskFailedError"]
    assert await handle.status() == "completed"

    events = list(await store.read_events(handle.run_id))
    assert _keys_of(events, EventType.TASK_FAILED) == ["n5"]
    store.close()


# -- 6. a crash is never a collected outcome ---------------------------------------


async def test_a_crash_inside_a_collect_map_still_propagates(temp_db_path: Path) -> None:
    """``SimulatedCrash`` models worker death — it aborts the composite, never a slot."""
    store = SQLiteStore.open(temp_db_path)
    injector = FaultInjector()
    injector.crash_after("TaskCompleted")

    handle = satay.start(collect_map_wf, [2, 4, 6], store=store, injector=injector)
    with pytest.raises(SimulatedCrash):
        await handle.result()
    assert await handle.status() == "running"  # non-terminal: the run is resumable
    store.close()
