"""Integration tests isolating the executor's retry loop (N10, ADR-0011).

Boundary-only per ADR-0011 (H3): the executor is driven directly with a capturing
commit hook, the manual clock, and the seeded RNG — no store, no engine.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from satay.api.decorators import TASK_ATTR, task
from satay.api.registry import TaskDefinition
from satay.executor import LocalTaskExecutor
from satay.journal.events import Event, EventType
from satay.replay.identity import CallIdentity
from satay.testing.clock import ManualClock
from satay.testing.rng import SeededRng

_ATTEMPTS: dict[str, int] = {}


@task(retries=2)
async def ex_flaky(value: int) -> int:
    _ATTEMPTS["ex_flaky"] = _ATTEMPTS.get("ex_flaky", 0) + 1
    if _ATTEMPTS["ex_flaky"] < 3:
        raise RuntimeError(f"ex_flaky boom #{_ATTEMPTS['ex_flaky']}")
    return value + 1


@task(retries=2)
async def ex_recover(value: int) -> int:
    _ATTEMPTS["ex_recover"] = _ATTEMPTS.get("ex_recover", 0) + 1
    return value + 1


def _defn(fn: Any) -> TaskDefinition:
    definition: TaskDefinition = getattr(fn, TASK_ATTR)
    return definition


class _Recorder:
    def __init__(self) -> None:
        self.events: list[Event] = []

    async def commit(self, event: Event) -> Event:
        stored = event.with_seq(len(self.events) + 1)
        self.events.append(stored)
        return stored


@pytest.fixture(autouse=True)
def _reset() -> None:
    _ATTEMPTS.clear()


async def test_executor_records_full_attempt_sequence_for_fail_twice_then_succeed(
    manual_clock: ManualClock,
    seeded_rng: SeededRng,
    drain: Callable[..., Awaitable[Any]],
) -> None:
    rec = _Recorder()
    executor = LocalTaskExecutor(rec.commit, clock=manual_clock, rng=seeded_rng)

    result = await drain(
        lambda: executor.execute(
            run_id="r1",
            definition=_defn(ex_flaky),
            identity=CallIdentity("ex_flaky", 0),
            args=(1,),
            kwargs={},
            key="k",
            prior_attempts=0,
            prior_failures=0,
        ),
        manual_clock,
    )

    assert result == 2
    assert [e.type for e in rec.events] == [
        EventType.TASK_ATTEMPT_STARTED,
        EventType.TASK_ATTEMPT_FAILED,
        EventType.TASK_ATTEMPT_STARTED,
        EventType.TASK_ATTEMPT_FAILED,
        EventType.TASK_ATTEMPT_STARTED,
        EventType.TASK_COMPLETED,
    ]
    # Attempts are numbered 1..N per logical task.
    starts = [e for e in rec.events if e.type is EventType.TASK_ATTEMPT_STARTED]
    assert [e.payload["attempt"] for e in starts] == [1, 2, 3]
    # Each non-terminal failure carries a bounded backoff delay; the last carries none.
    fails = [e for e in rec.events if e.type is EventType.TASK_ATTEMPT_FAILED]
    assert all(0.0 <= e.payload["next_delay"] <= 60.0 for e in fails)


async def test_resumed_attempt_continues_numbering_without_burning_budget(
    manual_clock: ManualClock,
    seeded_rng: SeededRng,
    drain: Callable[..., Awaitable[Any]],
) -> None:
    """A task resumed mid-flight (prior attempt started, no failure recorded) continues.

    The new attempt is numbered after the recorded one, and the retry budget is intact
    because no ``TaskAttemptFailed`` was recorded (at-least-once, not a burned retry).
    """
    rec = _Recorder()
    executor = LocalTaskExecutor(rec.commit, clock=manual_clock, rng=seeded_rng)

    result = await drain(
        lambda: executor.execute(
            run_id="r1",
            definition=_defn(ex_recover),
            identity=CallIdentity("ex_recover", 0),
            args=(10,),
            kwargs={},
            key="k",
            prior_attempts=1,  # one attempt was started before the crash
            prior_failures=0,  # but never recorded as failed
        ),
        manual_clock,
    )

    assert result == 11
    starts = [e for e in rec.events if e.type is EventType.TASK_ATTEMPT_STARTED]
    assert [e.payload["attempt"] for e in starts] == [2]  # numbering continues from 1
    assert rec.events[-1].type is EventType.TASK_COMPLETED
