"""Task execution with retry, backoff, and timeout (A4, N10).

The ``TaskExecutor`` seam and its production implementation ``LocalTaskExecutor``.
V1 ran a single attempt behind this seam precisely so V2 can add the retry loop here
**without touching the replay engine** (ADR: retry belongs in the executor). For each
logical task the executor:

- appends ``TaskAttemptStarted`` and runs the task coroutine (enforcing ``timeout``
  against the injected clock),
- on success appends ``TaskCompleted`` (flushing any ``ctx.record_model_usage`` into
  the generic usage slot) and returns,
- on failure appends ``TaskAttemptFailed`` (attempt / error / next_delay) and, while
  attempts remain, waits an exponential backoff with jitter (base 1s, cap ~60s) via
  the injected clock and RNG, then retries,
- after the last attempt fails, **re-raises the last error** so the engine records the
  terminal ``WorkflowFailed`` (keeping the engine the owner of terminal failure).

Every append goes through the injected commit hook, so the fault injector fires
post-commit (ADR-0011). Backoff waits go through the injected clock, so tests replay a
schedule with no real delay.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from satay.api.context import CURRENT_TASK_CONTEXT, TaskContext
from satay.api.registry import TaskDefinition
from satay.journal.codec import encode
from satay.journal.events import Event, EventType
from satay.replay.identity import CallIdentity, idempotency_key
from satay.testing.clock import Clock, RealClock
from satay.testing.faults import SimulatedCrash
from satay.testing.rng import Rng, SystemRng

#: The hook the executor calls to durably append an event (the engine supplies it;
#: it commits to the store and then fires the fault injector, ADR-0011).
CommitHook = Callable[[Event], Awaitable[Event]]

#: Exponential-backoff parameters (ADR-0006): base delay and cap, in seconds.
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_CAP_SECONDS = 60.0


def backoff_ceiling(
    failure: int, *, base: float = BACKOFF_BASE_SECONDS, cap: float = BACKOFF_CAP_SECONDS
) -> float:
    """The exponential ceiling for the ``failure``-th failure (1-based), capped.

    ``base * 2**(failure - 1)`` clamped to ``cap`` — 1s, 2s, 4s, … up to ~60s.
    """
    if failure < 1:
        raise ValueError("failure index is 1-based")
    return min(cap, base * (2.0 ** (failure - 1)))


def backoff_delay(
    failure: int,
    rng: Rng,
    *,
    base: float = BACKOFF_BASE_SECONDS,
    cap: float = BACKOFF_CAP_SECONDS,
) -> float:
    """A jittered backoff delay for the ``failure``-th failure (full jitter, ADR-0006).

    Returns a value in ``[0, backoff_ceiling(failure)]`` drawn from the injected RNG,
    so a seeded RNG makes the schedule exactly reproducible (ADR-0011, Q46).
    """
    return rng.uniform(0.0, backoff_ceiling(failure, base=base, cap=cap))


def detached_context(task_name: str) -> TaskContext:
    """A :class:`TaskContext` for a task called outside a drive (stays runnable)."""
    return TaskContext(
        run_id="",
        task_name=task_name,
        ordinal=0,
        attempt=1,
        idempotency_key=idempotency_key("", task_name, 0),
    )


class TaskExecutor(Protocol):
    """Executor seam (ARCHITECTURE §9). ``LocalTaskExecutor`` is the impl."""

    async def execute(
        self,
        *,
        run_id: str,
        definition: TaskDefinition,
        identity: CallIdentity,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        key: str,
        prior_attempts: int,
        prior_failures: int,
    ) -> Any:
        """Run a logical task to a durable result, retrying per its policy."""
        ...


class LocalTaskExecutor:
    """Runs a task on the asyncio loop with retry, backoff, and timeout (N10)."""

    def __init__(
        self,
        commit: CommitHook,
        *,
        clock: Clock | None = None,
        rng: Rng | None = None,
    ) -> None:
        self._commit = commit
        self._clock = clock or RealClock()
        self._rng = rng or SystemRng()

    async def execute(
        self,
        *,
        run_id: str,
        definition: TaskDefinition,
        identity: CallIdentity,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        key: str,
        prior_attempts: int,
        prior_failures: int,
    ) -> Any:
        """Run the task, retrying up to ``retries`` failures with jittered backoff.

        Returns the raw (un-encoded) task result. ``prior_attempts`` / ``prior_failures``
        continue a logical task interrupted mid-flight (at-least-once resume): attempt
        numbers stay monotonic, and only recorded failures consume the retry budget, so
        a crash between the effect and the result does not burn a retry.
        """
        max_attempts = definition.retries + 1
        attempt = prior_attempts + 1
        failures = prior_failures

        while True:
            await self._commit(
                Event(
                    run_id=run_id,
                    type=EventType.TASK_ATTEMPT_STARTED,
                    payload={**identity.payload_fields(), "attempt": attempt},
                    ts=self._clock.now(),
                )
            )
            ctx = TaskContext(
                run_id=run_id,
                task_name=identity.task_name,
                ordinal=identity.ordinal,
                attempt=attempt,
                idempotency_key=key,
            )

            # Bind the context for this attempt (inherited by the body task the timeout
            # race spawns, since ContextVars copy into a task at creation).
            token = CURRENT_TASK_CONTEXT.set(ctx)
            try:
                result = await self._run_attempt(definition, args, kwargs)
            except SimulatedCrash:
                # A simulated worker death is not a task failure: let it propagate.
                raise
            except Exception as exc:
                failures += 1
                remaining = max_attempts - failures
                next_delay = backoff_delay(failures, self._rng) if remaining > 0 else None
                await self._commit(
                    Event(
                        run_id=run_id,
                        type=EventType.TASK_ATTEMPT_FAILED,
                        payload={
                            **identity.payload_fields(),
                            "attempt": attempt,
                            "error": _error_payload(exc),
                            "next_delay": next_delay,
                        },
                        ts=self._clock.now(),
                    )
                )
                if remaining <= 0:
                    # Retries exhausted: re-raise so the engine records WorkflowFailed.
                    raise
                assert next_delay is not None
                await self._clock.sleep(next_delay)
                attempt += 1
                continue
            finally:
                CURRENT_TASK_CONTEXT.reset(token)

            usage = ctx.recorded_usage
            payload: dict[str, Any] = {
                **identity.payload_fields(),
                "output_ref": encode(result),
            }
            if usage:
                payload["usage"] = usage
            await self._commit(
                Event(
                    run_id=run_id,
                    type=EventType.TASK_COMPLETED,
                    payload=payload,
                    ts=self._clock.now(),
                )
            )
            return result

    async def _run_attempt(
        self,
        definition: TaskDefinition,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        """Run one attempt, enforcing ``timeout`` against the injected clock if set."""
        if definition.timeout is None:
            return await definition.fn(*args, **kwargs)

        body = asyncio.ensure_future(definition.fn(*args, **kwargs))
        timer = asyncio.ensure_future(self._clock.sleep(definition.timeout))
        try:
            await asyncio.wait({body, timer}, return_when=asyncio.FIRST_COMPLETED)
        except BaseException:  # pragma: no cover - cancellation of the executor
            body.cancel()
            timer.cancel()
            raise

        if body.done():
            timer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await timer
            return body.result()

        # The timeout fired first: cancel the body and fail the attempt.
        body.cancel()
        with contextlib.suppress(BaseException):
            await body
        raise TimeoutError(
            f"task {definition.name!r} exceeded its timeout of {definition.timeout}s"
        )


def _error_payload(exc: BaseException) -> dict[str, str]:
    """A compact error record for ``TaskAttemptFailed`` (type + message)."""
    return {"type": type(exc).__name__, "message": str(exc)}
