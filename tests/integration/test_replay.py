"""Integration tests for the replay engine's hit/miss reconciliation and guard (N6)."""

from __future__ import annotations

import pytest

from satay.api.decorators import task, workflow
from satay.api.primitives import start
from satay.journal.events import EventType
from satay.journal.store import SQLiteStore

# Distinct task names so this module's registry entries do not collide with demo/others.
EXEC: dict[str, int] = {}


@task()
async def ri_alpha(value: int) -> int:
    EXEC["alpha"] = EXEC.get("alpha", 0) + 1
    return value + 10


@task()
async def ri_beta(value: int) -> int:
    EXEC["beta"] = EXEC.get("beta", 0) + 1
    return value + 100


@workflow
async def ri_two_step(value: int) -> int:
    a = await ri_alpha(value)
    b = await ri_beta(a)
    return b


@workflow
async def ri_one_step(value: int) -> int:
    return await ri_alpha(value)


@workflow
async def ri_beta_first(value: int) -> int:
    return await ri_beta(value)


@pytest.fixture(autouse=True)
def _reset_exec() -> None:
    EXEC.clear()


async def test_miss_schedules_and_executes_and_records_completed() -> None:
    store = SQLiteStore.open(":memory:")
    handle = start(ri_two_step, 1, store=store)
    result = await handle.result()
    assert result == 111  # (1+10)=11, (11+100)=111
    assert EXEC == {"alpha": 1, "beta": 1}
    events = await store.read_events(handle.run_id)
    types = [e.type for e in events]
    assert EventType.TASK_SCHEDULED in types
    assert types.count(EventType.TASK_COMPLETED) == 2
    store.close()


async def test_hit_reuses_recorded_result_without_reexecuting() -> None:
    store = SQLiteStore.open(":memory:")
    # First drive completes the run.
    handle = start(ri_two_step, 1, store=store)
    await handle.result()
    assert EXEC == {"alpha": 1, "beta": 1}

    # A second start against the same (now terminal) run is a no-op: no re-execution.
    again = start(ri_two_step, 1, run_id=handle.run_id, store=store)
    result = await again.result()
    assert result == 111
    assert EXEC == {"alpha": 1, "beta": 1}  # unchanged — recorded results reused
    store.close()


async def test_determinism_guard_raises_on_task_name_collision() -> None:
    """A resume whose workflow issues a different task at a recorded position errors.

    V2 upgrades the V1 guard to the public ``NondeterminismError``; under ``strict`` it
    hard-fails (the default ``warn`` mode logs and continues — covered in the E2E tier).
    """
    from satay.api import NondeterminismError

    store = SQLiteStore.open(":memory:")
    # Drive ri_one_step, crashing after ri_alpha's TaskCompleted so the run is left
    # non-terminal with ri_alpha recorded at durable-call position 0.
    from satay.testing.faults import FaultInjector, SimulatedCrash

    inj = FaultInjector()
    inj.crash_after("TaskCompleted")
    handle = start(ri_one_step, 1, store=store, injector=inj)
    with pytest.raises(SimulatedCrash):
        await handle.result()

    # Resume the SAME run_id with a workflow that issues ri_beta first — a different
    # task name at the recorded position 0.
    resumed = start(ri_beta_first, 1, run_id=handle.run_id, store=store, effect_safety="strict")
    with pytest.raises(NondeterminismError, match="nondeterministic"):
        await resumed.result()
    store.close()
