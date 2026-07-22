"""Task execution (A4).

The ``TaskExecutor`` seam and its only MVP implementation, ``LocalTaskExecutor``,
which runs a task coroutine on the loop and (from V2) applies retry with exponential
backoff and jitter, driving all timing through the **injected clock** and jitter
through the **injected RNG** so it is deterministically testable (ADR-0011, ADR-0019).

Scaffold: the seam is declared; ``LocalTaskExecutor`` lands in V1, retry/backoff in V2.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol


class TaskExecutor(Protocol):
    """Executor seam (ARCHITECTURE §9). ``LocalTaskExecutor`` lands in V1."""

    async def run_task(
        self,
        task: Callable[..., Awaitable[Any]],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Run a task attempt to completion (retry/backoff added in V2)."""
        ...
