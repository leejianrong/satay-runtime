"""Root test configuration.

Loads the first-class testing-seam fixtures (ADR-0011) as a pytest plugin so every
tier can inject the manual clock, seeded RNG, fault injector, and temp-store paths.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from satay.testing.clock import ManualClock

pytest_plugins = ["satay.testing.fixtures"]


@pytest.fixture
def drain() -> Callable[..., Awaitable[Any]]:
    """Drive an awaitable to completion, advancing a ``ManualClock`` through backoff.

    Runs the coroutine returned by ``factory`` as a task and, whenever it suspends on a
    clock sleeper (a backoff wait or a timeout race), advances virtual time enough to
    fire it — so a full retry schedule replays with zero real delay (ADR-0011).
    """

    async def _drain(
        factory: Callable[[], Awaitable[Any]],
        clock: ManualClock,
        *,
        step: float = 61.0,
        max_steps: int = 2000,
    ) -> Any:
        task = asyncio.ensure_future(factory())
        try:
            for _ in range(max_steps):
                for _ in range(4):
                    await asyncio.sleep(0)
                if task.done():
                    return await task
                if clock.pending_sleepers:
                    clock.advance(step)
        finally:
            if not task.done():
                task.cancel()
        raise AssertionError("awaitable did not settle within max_steps")

    return _drain
