"""The shared drive-under-a-manual-clock helper, :func:`satay.testing.settle` (KAN-482).

Two halves, and the second one is the one that matters. Settling a jittered retry schedule
in zero real time is what the helper is *for*; refusing to hang when the run genuinely
cannot settle is what makes it safe to hand to an example script. A helper that spins
forever is strictly worse than the four copies of the loop it replaces, because the copies
at least failed in a file you were already reading.

Driven through the public seam against a temp store, asserting observable outcomes only
(ADR-0011).
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

import satay
from satay import PARKED
from satay.journal.events import EventType
from satay.journal.store import SQLiteStore
from satay.testing import ManualClock, NeverSettledError, SeededRng, settle

#: Real seconds the whole module allows itself. Every wait here is virtual, so anything
#: approaching this ceiling means ``settle`` waited on the wall clock — the exact bug.
REAL_TIME_BUDGET_SECONDS = 5.0

_EXECUTIONS: dict[str, int] = {}


@pytest.fixture(autouse=True)
def _reset() -> None:
    _EXECUTIONS.clear()


def _ran(name: str) -> int:
    _EXECUTIONS[name] = _EXECUTIONS.get(name, 0) + 1
    return _EXECUTIONS[name]


@satay.task(retries=3)
async def st_flaky_fetch(value: int) -> int:
    """Fails three times, succeeds on the fourth — three real backoff waits to advance."""
    if _ran("st_flaky_fetch") < 4:
        raise RuntimeError("upstream timed out")
    return value + 1


@satay.workflow
async def st_flaky_quote(value: int) -> int:
    return await st_flaky_fetch(value)


@satay.task()
async def st_waits_on_real_time(value: int) -> int:
    """Sleeps on ``asyncio``, not on the injected clock — so no advance can wake it.

    This is the mistake ``settle`` exists to diagnose: a task body that reaches around the
    clock seam. Under a ``ManualClock`` the drive is suspended on something virtual time
    cannot reach, and the helper has to say so rather than spin.
    """
    await asyncio.sleep(3600)
    return value


@satay.workflow
async def st_unsettleable(value: int) -> int:
    return await st_waits_on_real_time(value)


# -- the half that settles -------------------------------------------------------------


async def test_settle_replays_a_jittered_backoff_schedule_with_no_real_waiting() -> None:
    """Three capped, full-jitter backoff waits resolve in virtual time (ADR-0006)."""
    clock = ManualClock()
    store = SQLiteStore.open(":memory:")
    try:
        handle = satay.start(st_flaky_quote, 41, store=store, clock=clock, rng=SeededRng(1234))
        started = time.monotonic()
        result = await settle(handle.result, clock)
        real_elapsed = time.monotonic() - started

        assert result == 42
        assert _EXECUTIONS["st_flaky_fetch"] == 4  # the schedule really was walked
        assert real_elapsed < REAL_TIME_BUDGET_SECONDS

        events = await store.read_events(handle.run_id)
        failures = [e for e in events if e.type is EventType.TASK_ATTEMPT_FAILED]
        delays = [e.payload["next_delay"] for e in failures]
        assert len(delays) == 3
        assert all(0.0 <= delay <= 60.0 for delay in delays)  # capped
        # The delays were real and non-zero, i.e. the drive did suspend on the clock and
        # was woken by ``settle`` rather than never having waited at all.
        assert sum(delays) > 0.0
        assert clock.monotonic() > sum(delays)
    finally:
        store.close()


async def test_settle_takes_a_coroutine_as_well_as_a_factory() -> None:
    """Both call shapes work: the examples pass a callable, ``settle(coro, clock)`` also."""
    clock = ManualClock()
    store = SQLiteStore.open(":memory:")
    try:
        handle = satay.start(st_flaky_quote, 1, store=store, clock=clock, rng=SeededRng(7))
        assert await settle(handle.result(), clock) == 2
        # Same run, already terminal: the second drive returns the recorded result.
        assert await settle(handle.result, clock) == 2
    finally:
        store.close()


async def test_settle_hands_back_a_run_that_parks_rather_than_waiting_for_a_worker() -> None:
    """A durable ``sleep`` parks the run; parking is a result, not a stall."""
    clock = ManualClock()
    store = SQLiteStore.open(":memory:")
    try:
        handle = satay.start(_st_sleeper, 3, store=store, clock=clock)
        assert await settle(handle.result, clock) is PARKED
        assert await handle.status() == "waiting"
    finally:
        store.close()


@satay.workflow
async def _st_sleeper(value: int) -> int:
    await satay.sleep(8 * 3600)
    return value


# -- the half that must not hang -------------------------------------------------------


async def test_settle_raises_instead_of_hanging_when_nothing_can_wake_the_drive() -> None:
    """Nothing is on the clock and the drive never finishes: give up, do not spin."""
    never = asyncio.Event()
    clock = ManualClock()

    started = time.monotonic()
    with pytest.raises(NeverSettledError) as excinfo:
        await settle(never.wait, clock, max_steps=50)
    real_elapsed = time.monotonic() - started

    assert real_elapsed < REAL_TIME_BUDGET_SECONDS
    message = str(excinfo.value)
    assert "never settled" in message
    assert "real time" in message  # points at the actual cause
    assert "50 passes" in message  # and at the ceiling it hit


async def test_settle_gives_up_on_a_task_body_that_sleeps_on_real_time() -> None:
    """The realistic version of the above: a task body that bypasses the clock seam."""
    clock = ManualClock()
    store = SQLiteStore.open(":memory:")
    try:
        handle = satay.start(st_unsettleable, 1, store=store, clock=clock)
        started = time.monotonic()
        with pytest.raises(NeverSettledError):
            await settle(handle.result, clock, max_steps=100)
        assert time.monotonic() - started < REAL_TIME_BUDGET_SECONDS
    finally:
        store.close()


async def test_settle_cancels_the_drive_it_gave_up_on() -> None:
    """Giving up must not leave the drive running behind the caller's back."""
    cancelled = asyncio.Event()

    async def forever() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    with pytest.raises(NeverSettledError):
        await settle(forever, ManualClock(), max_steps=20)

    await asyncio.sleep(0)  # let the cancellation land
    assert cancelled.is_set()


async def test_settle_propagates_what_the_drive_raises() -> None:
    """A failure is not a stall: the run's own error comes through untouched."""
    clock = ManualClock()
    store = SQLiteStore.open(":memory:")
    try:
        handle = satay.start(_st_doomed, 1, store=store, clock=clock, rng=SeededRng(1234))
        with pytest.raises(satay.WorkflowFailedError) as excinfo:
            await settle(handle.result, clock)
        assert excinfo.value.error_type == "ConnectionError"
    finally:
        store.close()


@satay.task(retries=1)
async def _st_always_fails(value: int) -> int:
    _ran("_st_always_fails")
    raise ConnectionError("no route to host")


@satay.workflow
async def _st_doomed(value: int) -> int:
    return await _st_always_fails(value)


# -- one implementation, not two -------------------------------------------------------


def test_the_drain_fixture_is_the_public_settle_function(
    drain: Callable[..., Awaitable[Any]],
) -> None:
    """The fixture delegates rather than re-implementing, so the two cannot drift."""
    assert drain is settle


def test_settle_is_part_of_the_satay_testing_surface() -> None:
    import satay.testing as testing

    assert "settle" in testing.__all__
    assert "NeverSettledError" in testing.__all__


def test_never_settled_error_is_an_assertion_error() -> None:
    """Preserves how the ``drain`` fixture used to fail, so tests still read as tests."""
    assert issubclass(NeverSettledError, AssertionError)


def test_importing_settle_does_not_require_pytest() -> None:
    """``satay.testing`` is imported by six core modules, so it must stay pytest-free.

    The fixture lives in ``satay.testing.fixtures`` and imports pytest; ``settle`` lives
    next to it and must not. Checked in a fresh interpreter because this one obviously has
    pytest loaded already.
    """
    program = (
        "import sys, satay.testing; "
        "assert callable(satay.testing.settle); "
        "assert 'pytest' not in sys.modules, sorted(sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
