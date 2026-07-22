"""Injectable clock (ADR-0011).

Time is a first-class, injectable dependency so ``sleep``, timeouts, and backoff are
testable without real waiting. ``RealClock`` is the production default; ``ManualClock``
advances virtual time under test control. Both satisfy the ``Clock`` protocol, which
is what the executor and timer loop depend on.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """The clock seam depended on by the executor and timer loop (ADR-0011)."""

    def now(self) -> datetime:
        """Return the current wall-clock time (timezone-aware, UTC)."""
        ...

    def monotonic(self) -> float:
        """Return a monotonic time in seconds (for measuring durations)."""
        ...

    async def sleep(self, seconds: float) -> None:
        """Sleep for ``seconds`` of this clock's time."""
        ...


class RealClock:
    """Wall-clock / event-loop backed clock. The production default."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class ManualClock:
    """Virtual clock advanced explicitly by tests.

    ``now()`` and ``monotonic()`` reflect only the virtual time accumulated via
    ``advance``. ``sleep`` suspends until virtual time reaches the deadline, so a
    test drives all waiting deterministically by calling ``advance``.
    """

    def __init__(self, start: datetime | None = None) -> None:
        if start is None:
            start = datetime(2026, 1, 1, tzinfo=UTC)
        if start.tzinfo is None:
            raise ValueError("ManualClock start must be timezone-aware")
        self._start = start
        self._elapsed = 0.0
        self._sleepers: list[tuple[float, asyncio.Future[None]]] = []

    def now(self) -> datetime:
        return self._start + timedelta(seconds=self._elapsed)

    def monotonic(self) -> float:
        return self._elapsed

    def advance(self, seconds: float) -> None:
        """Advance virtual time by ``seconds`` and wake any due sleepers."""
        if seconds < 0:
            raise ValueError("cannot advance time backwards")
        self._elapsed += seconds
        due = [(deadline, fut) for (deadline, fut) in self._sleepers if deadline <= self._elapsed]
        self._sleepers = [
            (deadline, fut) for (deadline, fut) in self._sleepers if deadline > self._elapsed
        ]
        for _deadline, fut in due:
            if not fut.done():
                fut.set_result(None)

    async def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            return
        deadline = self._elapsed + seconds
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[None] = loop.create_future()
        entry = (deadline, fut)
        self._sleepers.append(entry)
        try:
            await fut
        except asyncio.CancelledError:
            # A cancelled sleeper (e.g. a timeout race the body won) must not linger in
            # ``pending_sleepers`` — drop it so test introspection stays accurate.
            with contextlib.suppress(ValueError):
                self._sleepers.remove(entry)
            raise

    @property
    def pending_sleepers(self) -> int:
        """Number of coroutines currently suspended in ``sleep`` (test introspection)."""
        return len(self._sleepers)
