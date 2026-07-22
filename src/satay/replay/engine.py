"""The replay engine (N6) — re-runs a workflow and reconciles it with the journal.

Given a ``run_id``, the engine loads the ordered journal, then re-runs the workflow
coroutine top-to-bottom. On each awaited durable call it resolves identity
(``(task_name, ordinal)``, N7) and consults the journal:

- **hit** — a ``TaskCompleted`` exists for this identity: return the rehydrated
  recorded result *without executing* the task (once-recorded logical completion).
- **miss** — no ``TaskCompleted`` yet. This covers both "clean, not yet started" and
  the **ambiguous, in-flight** case (a ``TaskAttemptStarted`` with no terminal result,
  ADR-0006/A4.4): the attempt's fate is unknown, so the task re-runs (at-least-once).
  The engine appends ``TaskScheduled`` (once), then hands the call to the executor,
  which owns the retry loop; recorded attempt/failure counts continue the numbering.

On workflow return it appends ``WorkflowCompleted``; on a native workflow/task error
(including retry exhaustion re-raised by the executor) it appends ``WorkflowFailed``.
A ``SimulatedCrash``, ``NondeterminismError``, or ``EffectSafetyError`` is allowed to
propagate unrecorded — a crash models worker death; the latter two are dev-time
divergence/policy failures the developer resolves before re-driving.

**Nondeterminism (N9).** If a durable call's task name does not match the journal
entry at that global position, the engine raises :class:`NondeterminismError`
(expected-vs-actual). Policy follows the effect-safety mode (ADR-0003): ``strict``
fails, ``warn`` logs and continues, ``off`` is silent.
"""

from __future__ import annotations

import inspect
import logging
import traceback
from typing import Any, get_type_hints

from satay.api.registry import TaskDefinition, WorkflowDefinition
from satay.config import EffectSafety
from satay.executor import LocalTaskExecutor, TaskExecutor
from satay.journal import Store
from satay.journal.codec import encode, rehydrate
from satay.journal.events import Event, EventType, RunStatus
from satay.replay.driver import CURRENT_DRIVER
from satay.replay.identity import CallIdentity, IdentityResolver, idempotency_key
from satay.replay.nondeterminism import EffectSafetyError, NondeterminismError
from satay.testing.clock import Clock, RealClock
from satay.testing.faults import FaultInjector, SimulatedCrash
from satay.testing.rng import Rng, SystemRng

_LOG = logging.getLogger("satay")

#: Errors that model an out-of-band stop, propagated unrecorded from ``drive``.
_PROPAGATE = (SimulatedCrash, NondeterminismError, EffectSafetyError)


class ReplayEngine:
    """Drives one run: replays recorded durable calls, executes the misses."""

    def __init__(
        self,
        *,
        store: Store,
        run_id: str,
        injector: FaultInjector | None = None,
        clock: Clock | None = None,
        rng: Rng | None = None,
        executor: TaskExecutor | None = None,
        effect_safety: EffectSafety = EffectSafety.WARN,
    ) -> None:
        self._store = store
        self._run_id = run_id
        self._injector = injector
        self._clock = clock or RealClock()
        self._rng = rng or SystemRng()
        self._effect_safety = effect_safety
        self._executor = executor or LocalTaskExecutor(
            self._commit, clock=self._clock, rng=self._rng
        )

        self._resolver = IdentityResolver()
        self._call_index = 0
        self._completed: dict[CallIdentity, Any] = {}
        self._scheduled: set[CallIdentity] = set()
        self._schedule_order: list[str] = []
        #: Highest recorded attempt number per identity (continues numbering on resume).
        self._max_attempt: dict[CallIdentity, int] = {}
        #: Recorded ``TaskAttemptFailed`` count per identity (consumes the retry budget).
        self._failures: dict[CallIdentity, int] = {}

    async def _commit(self, event: Event) -> Event:
        """Append an event, then fire the fault injector after the commit (ADR-0011)."""
        stored = await self._store.append(event)
        if self._injector is not None:
            await self._injector.reached(stored.type.value)
        return stored

    def _load_journal(self, events: list[Event]) -> None:
        for event in events:
            payload = event.payload
            if event.type is EventType.TASK_SCHEDULED:
                identity = CallIdentity(payload["task_name"], payload["ordinal"])
                self._scheduled.add(identity)
                self._schedule_order.append(payload["task_name"])
            elif event.type is EventType.TASK_ATTEMPT_STARTED:
                identity = CallIdentity(payload["task_name"], payload["ordinal"])
                attempt = int(payload.get("attempt", 1))
                self._max_attempt[identity] = max(self._max_attempt.get(identity, 0), attempt)
            elif event.type is EventType.TASK_ATTEMPT_FAILED:
                identity = CallIdentity(payload["task_name"], payload["ordinal"])
                attempt = int(payload.get("attempt", 1))
                self._max_attempt[identity] = max(self._max_attempt.get(identity, 0), attempt)
                self._failures[identity] = self._failures.get(identity, 0) + 1
            elif event.type is EventType.TASK_COMPLETED:
                identity = CallIdentity(payload["task_name"], payload["ordinal"])
                self._completed[identity] = payload["output_ref"]

    # -- Driver protocol ---------------------------------------------------------

    async def durable_call(
        self,
        definition: TaskDefinition,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        """Intercept a task call: reuse a recorded result or schedule + execute it."""
        identity = self._resolver.next(definition.name)

        # Nondeterminism check (N9): the task name issued at this global durable-call
        # position must match what the journal recorded there.
        position = self._call_index
        self._call_index += 1
        if position < len(self._schedule_order):
            recorded_name = self._schedule_order[position]
            if recorded_name != definition.name:
                self._on_nondeterminism(position, expected=recorded_name, actual=definition.name)

        if identity in self._completed:
            # Hit: rehydrate the recorded result; do NOT execute the task.
            return rehydrate(self._completed[identity], _return_annotation(definition.fn))

        # Miss (clean or ambiguous-in-flight): enforce effect safety, then schedule +
        # execute. A recorded TaskScheduled is not re-appended (mid-task crash).
        self._enforce_effect_safety(definition)
        if identity not in self._scheduled:
            await self._commit(
                Event(
                    run_id=self._run_id,
                    type=EventType.TASK_SCHEDULED,
                    payload={
                        "task_name": identity.task_name,
                        "ordinal": identity.ordinal,
                        "input_ref": encode(list(args)),
                    },
                    ts=self._clock.now(),
                )
            )
            self._scheduled.add(identity)

        key = idempotency_key(self._run_id, identity.task_name, identity.ordinal)
        return await self._executor.execute(
            run_id=self._run_id,
            definition=definition,
            identity=identity,
            args=args,
            kwargs=kwargs,
            key=key,
            prior_attempts=self._max_attempt.get(identity, 0),
            prior_failures=self._failures.get(identity, 0),
        )

    # -- policy ------------------------------------------------------------------

    def _on_nondeterminism(self, position: int, *, expected: str, actual: str) -> None:
        error = NondeterminismError(position=position, expected=expected, actual=actual)
        if self._effect_safety is EffectSafety.STRICT:
            raise error
        if self._effect_safety is EffectSafety.WARN:
            # Dev warns; the offer-to-fork recovery path lands in V7.
            _LOG.warning("%s", error)
        # off: silent. Fall through — the divergent call proceeds as a fresh miss.

    def _enforce_effect_safety(self, definition: TaskDefinition) -> None:
        """Reject/warn on an unguarded retryable side-effecting task (A10.2)."""
        if not (definition.side_effect and definition.retries > 0):
            return
        if definition.is_effect_guarded:
            return
        if self._effect_safety is EffectSafety.STRICT:
            raise EffectSafetyError(definition.name)
        if self._effect_safety is EffectSafety.WARN:
            _LOG.warning(
                "effect_safety: task %r is side-effecting and retryable but declares no "
                "idempotency or compensation strategy (set @task(idempotent=True) or accept "
                "a ctx parameter)",
                definition.name,
            )
        # off: silent.

    # -- drive -------------------------------------------------------------------

    async def drive(self, workflow_def: WorkflowDefinition, workflow_input: Any) -> None:
        """Re-run the workflow to a terminal state, reconciling with the journal."""
        events = list(await self._store.read_events(self._run_id))
        self._load_journal(events)

        token = CURRENT_DRIVER.set(self)
        try:
            result = await workflow_def.fn(workflow_input)
        except Exception as exc:
            # SimulatedCrash models worker death; NondeterminismError / EffectSafetyError
            # are dev-time failures — all propagate unrecorded for the caller to resolve.
            if isinstance(exc, _PROPAGATE):
                raise
            await self._commit(
                Event(
                    run_id=self._run_id,
                    type=EventType.WORKFLOW_FAILED,
                    payload={
                        "error": {
                            "type": type(exc).__name__,
                            "message": str(exc),
                            "traceback": "".join(
                                traceback.format_exception(type(exc), exc, exc.__traceback__)
                            ),
                        }
                    },
                    ts=self._clock.now(),
                )
            )
            await self._store.set_status(self._run_id, RunStatus.FAILED)
        else:
            await self._commit(
                Event(
                    run_id=self._run_id,
                    type=EventType.WORKFLOW_COMPLETED,
                    payload={"output_ref": encode(result)},
                    ts=self._clock.now(),
                )
            )
            await self._store.set_status(self._run_id, RunStatus.COMPLETED)
        finally:
            CURRENT_DRIVER.reset(token)


def _return_annotation(fn: Any) -> Any:
    """Best-effort resolved return annotation of ``fn`` (``None`` if absent/unresolvable)."""
    try:
        hints = get_type_hints(fn)
    except Exception:
        sig = inspect.signature(fn)
        ann = sig.return_annotation
        return None if ann is inspect.Signature.empty else ann
    return hints.get("return")
