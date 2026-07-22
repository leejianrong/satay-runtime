"""E2E: retries with exponential backoff, timeout, and terminal exhaustion (N10).

Driven through the public seam with the manual clock (pins backoff *timing*) and the
seeded RNG (pins backoff *jitter*), so a full retry schedule replays with no real delay
(ADR-0011). The ``drain`` fixture advances virtual time whenever the drive suspends on
a backoff or timeout sleeper.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from satay import demo
from satay.api.decorators import task, workflow
from satay.api.primitives import start
from satay.api.run_handle import WorkflowFailedError
from satay.journal.events import EventType
from satay.journal.store import SQLiteStore
from satay.testing.clock import ManualClock
from satay.testing.rng import SeededRng

_AF: list[int] = []
_TO: list[int] = []


@task(retries=2)
async def af_always_fail(value: int) -> int:
    _AF.append(1)
    raise ValueError(f"nope #{len(_AF)}")


@workflow
async def af_wf(value: int) -> int:
    return await af_always_fail(value)


@task(timeout=5.0, retries=1)
async def to_hang(value: int) -> int:
    _TO.append(1)
    await asyncio.sleep(3600)  # real sleep; cancelled by the timeout race
    return value


@workflow
async def to_wf(value: int) -> int:
    return await to_hang(value)


@pytest.fixture(autouse=True)
def _reset() -> None:
    demo.reset_executions()
    _AF.clear()
    _TO.clear()


async def test_attempts_are_recorded_with_bounded_jittered_backoff(
    drain: Callable[..., Awaitable[Any]],
) -> None:
    clock = ManualClock()
    store = SQLiteStore.open(":memory:")
    handle = start(demo.flaky_demo, 5, store=store, clock=clock, rng=SeededRng(1234))

    result = await drain(lambda: handle.result(), clock)

    assert result == 6  # 5 + 1 on the third attempt
    assert demo.execution_count("flaky_thrice") == 3
    events = await store.read_events(handle.run_id)
    starts = [e for e in events if e.type is EventType.TASK_ATTEMPT_STARTED]
    fails = [e for e in events if e.type is EventType.TASK_ATTEMPT_FAILED]
    completed = [e for e in events if e.type is EventType.TASK_COMPLETED]
    assert [e.payload["attempt"] for e in starts] == [1, 2, 3]
    assert len(fails) == 2 and len(completed) == 1
    for i, f in enumerate(fails, start=1):
        assert 0.0 <= f.payload["next_delay"] <= demo_ceiling(i)
    store.close()


def demo_ceiling(failure: int) -> float:
    from satay.executor import backoff_ceiling

    return backoff_ceiling(failure)


async def test_backoff_schedule_is_reproducible_under_the_seeded_rng(
    drain: Callable[..., Awaitable[Any]],
) -> None:
    async def run_once(seed: int) -> list[float]:
        demo.reset_executions()
        clock = ManualClock()
        store = SQLiteStore.open(":memory:")
        handle = start(demo.flaky_demo, 5, store=store, clock=clock, rng=SeededRng(seed))
        await drain(lambda: handle.result(), clock)
        events = await store.read_events(handle.run_id)
        delays = [
            e.payload["next_delay"] for e in events if e.type is EventType.TASK_ATTEMPT_FAILED
        ]
        store.close()
        return delays

    assert await run_once(1234) == await run_once(1234)  # reproducible
    assert await run_once(1234) != await run_once(4321)  # seed-dependent


async def test_timeout_fails_the_attempt_and_retries_then_exhausts(
    drain: Callable[..., Awaitable[Any]],
) -> None:
    clock = ManualClock()
    store = SQLiteStore.open(":memory:")
    handle = start(to_wf, 1, store=store, clock=clock, rng=SeededRng(1234))

    with pytest.raises(WorkflowFailedError) as excinfo:
        await drain(lambda: handle.result(), clock)

    assert excinfo.value.error_type == "TimeoutError"
    assert len(_TO) == 2  # timed out, retried once, timed out again
    events = await store.read_events(handle.run_id)
    fails = [e for e in events if e.type is EventType.TASK_ATTEMPT_FAILED]
    assert len(fails) == 2
    assert all(f.payload["error"]["type"] == "TimeoutError" for f in fails)
    assert events[-1].type is EventType.WORKFLOW_FAILED
    store.close()


async def test_retry_exhaustion_records_workflow_failed_with_last_error(
    drain: Callable[..., Awaitable[Any]],
) -> None:
    store = SQLiteStore.open(":memory:")
    clock = ManualClock()
    handle = start(af_wf, 1, store=store, clock=clock, rng=SeededRng(1234))

    with pytest.raises(WorkflowFailedError) as excinfo:
        await drain(lambda: handle.result(), clock)

    assert excinfo.value.error_type == "ValueError"
    assert "nope #3" in excinfo.value.error_message  # the last attempt's error
    assert len(_AF) == 3  # retries=2 → three attempts
    events = await store.read_events(handle.run_id)
    assert len([e for e in events if e.type is EventType.TASK_ATTEMPT_FAILED]) == 3
    assert events[-1].type is EventType.WORKFLOW_FAILED
    store.close()


async def test_attempts_render_in_the_text_timeline(
    drain: Callable[..., Awaitable[Any]],
) -> None:
    """The recorded attempts/failures are visible in the shared timeline view (U1/V6)."""
    store = SQLiteStore.open(":memory:")
    clock = ManualClock()
    handle = start(af_wf, 1, store=store, clock=clock, rng=SeededRng(1234))
    with pytest.raises(WorkflowFailedError):
        await drain(lambda: handle.result(), clock)

    from satay.journal.timeline import render_timeline

    events = list(await store.read_events(handle.run_id))
    text = render_timeline(events, run_id=handle.run_id)
    assert "TaskAttemptFailed" in text
    assert "attempt=3" in text
    store.close()
