"""Unit tests for the fault-injection hook (ADR-0011)."""

from __future__ import annotations

import asyncio

import pytest

from satay.testing.faults import FaultInjector, SimulatedCrash


async def test_reached_is_noop_without_armed_faults() -> None:
    injector = FaultInjector()
    await injector.reached("TaskCompleted")  # does not raise or block


async def test_crash_after_raises_once() -> None:
    injector = FaultInjector()
    injector.crash_after("TaskCompleted")

    with pytest.raises(SimulatedCrash) as excinfo:
        await injector.reached("TaskCompleted")
    assert excinfo.value.event_type == "TaskCompleted"

    # Fault clears itself after firing.
    await injector.reached("TaskCompleted")


async def test_crash_after_fires_configured_number_of_times() -> None:
    injector = FaultInjector()
    injector.crash_after("TaskAttemptStarted", times=2)

    with pytest.raises(SimulatedCrash):
        await injector.reached("TaskAttemptStarted")
    with pytest.raises(SimulatedCrash):
        await injector.reached("TaskAttemptStarted")
    await injector.reached("TaskAttemptStarted")  # third does not raise


async def test_stall_blocks_until_released() -> None:
    injector = FaultInjector()
    injector.stall_after("WorkflowCompleted")

    task = asyncio.ensure_future(injector.reached("WorkflowCompleted"))
    await asyncio.sleep(0)
    assert not task.done()

    injector.release("WorkflowCompleted")
    await asyncio.wait_for(task, timeout=1.0)


async def test_clear_releases_stalls_and_removes_crashes() -> None:
    injector = FaultInjector()
    injector.crash_after("TaskCompleted")
    injector.stall_after("WorkflowCompleted")

    task = asyncio.ensure_future(injector.reached("WorkflowCompleted"))
    await asyncio.sleep(0)

    injector.clear()
    await asyncio.wait_for(task, timeout=1.0)
    await injector.reached("TaskCompleted")  # crash was cleared
