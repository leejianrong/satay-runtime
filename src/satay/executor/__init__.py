"""Task execution (A4).

The ``TaskExecutor`` seam and its only V1 implementation, ``LocalTaskExecutor``,
which runs a task coroutine on the asyncio loop as a **single attempt** (retry with
backoff and jitter — driven by the injected clock/RNG — lands in V2). It appends
``TaskAttemptStarted`` before running and ``TaskCompleted`` after success, both via
the injected commit hook so the fault injector fires post-commit (ADR-0011). A task
exception propagates to the replay engine, which records ``WorkflowFailed`` (no retry
in V1).

Living behind the seam from day one means V2 adds retry inside the executor without
touching the replay engine.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from satay.api.registry import TaskDefinition
from satay.journal.codec import encode
from satay.journal.events import Event, EventType
from satay.replay.identity import CallIdentity
from satay.testing.clock import Clock, RealClock

#: The hook the executor calls to durably append an event (the engine supplies it;
#: it commits to the store and then fires the fault injector, ADR-0011).
CommitHook = Callable[[Event], Awaitable[Event]]


class TaskExecutor(Protocol):
    """Executor seam (ARCHITECTURE §9). ``LocalTaskExecutor`` is the V1 impl."""

    async def execute(
        self,
        *,
        run_id: str,
        definition: TaskDefinition,
        identity: CallIdentity,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        """Run a task attempt to completion, journalling start and completion."""
        ...


class LocalTaskExecutor:
    """Runs a task on the asyncio loop, single-attempt (N10)."""

    def __init__(self, commit: CommitHook, *, clock: Clock | None = None) -> None:
        self._commit = commit
        self._clock = clock or RealClock()

    async def execute(
        self,
        *,
        run_id: str,
        definition: TaskDefinition,
        identity: CallIdentity,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        """Append ``TaskAttemptStarted``, run the task, append ``TaskCompleted``.

        Returns the raw (un-encoded) task result. A task exception propagates to the
        caller (the engine) unrecorded here; V1 has no retry loop.
        """
        await self._commit(
            Event(
                run_id=run_id,
                type=EventType.TASK_ATTEMPT_STARTED,
                payload={
                    "task_name": identity.task_name,
                    "ordinal": identity.ordinal,
                    "attempt": 1,
                },
                ts=self._clock.now(),
            )
        )

        result = await definition.fn(*args, **kwargs)

        await self._commit(
            Event(
                run_id=run_id,
                type=EventType.TASK_COMPLETED,
                payload={
                    "task_name": identity.task_name,
                    "ordinal": identity.ordinal,
                    "output_ref": encode(result),
                },
                ts=self._clock.now(),
            )
        )
        return result
