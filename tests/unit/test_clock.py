"""Unit tests for the injectable clock (ADR-0011)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from satay.testing.clock import Clock, ManualClock, RealClock


def test_real_and_manual_satisfy_protocol() -> None:
    assert isinstance(RealClock(), Clock)
    assert isinstance(ManualClock(), Clock)


def test_manual_clock_advances_deterministically() -> None:
    start = datetime(2026, 7, 22, tzinfo=UTC)
    clock = ManualClock(start=start)

    assert clock.now() == start
    assert clock.monotonic() == 0.0

    clock.advance(1.5)
    clock.advance(0.5)

    assert clock.monotonic() == 2.0
    assert clock.now() == start + timedelta(seconds=2.0)


def test_manual_clock_is_reproducible() -> None:
    start = datetime(2026, 7, 22, tzinfo=UTC)
    a = ManualClock(start=start)
    b = ManualClock(start=start)
    for step in (1.0, 2.5, 0.25):
        a.advance(step)
        b.advance(step)
    assert a.now() == b.now()
    assert a.monotonic() == b.monotonic()


async def test_manual_clock_sleep_resolves_on_advance() -> None:
    clock = ManualClock()
    woke: list[float] = []

    async def sleeper() -> None:
        await clock.sleep(5.0)
        woke.append(clock.monotonic())

    task = asyncio.ensure_future(sleeper())
    await asyncio.sleep(0)  # let the sleeper suspend

    assert clock.pending_sleepers == 1
    assert not task.done()

    clock.advance(3.0)
    await asyncio.sleep(0)
    assert not task.done()  # deadline not yet reached

    clock.advance(2.0)
    await task
    assert woke == [5.0]
    assert clock.pending_sleepers == 0


async def test_manual_clock_sleep_zero_returns_immediately() -> None:
    clock = ManualClock()
    await clock.sleep(0)
    assert clock.pending_sleepers == 0
