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
import uuid
from datetime import timedelta
from typing import Any, get_type_hints

from satay.api.registry import TaskDefinition, WorkflowDefinition
from satay.config import EffectSafety
from satay.executor import LocalTaskExecutor, TaskExecutor
from satay.journal import Store
from satay.journal.codec import encode, rehydrate
from satay.journal.events import (
    Event,
    EventType,
    RunStatus,
    TimerKind,
    TimerRecord,
    TimerStatus,
)
from satay.replay.driver import CURRENT_DRIVER
from satay.replay.identity import CallIdentity, IdentityResolver, idempotency_key
from satay.replay.nondeterminism import EffectSafetyError, NondeterminismError
from satay.testing.clock import Clock, RealClock
from satay.testing.faults import FaultInjector, SimulatedCrash
from satay.testing.rng import Rng, SystemRng

_LOG = logging.getLogger("satay")


class WorkflowParked(BaseException):
    """Internal signal: a durable primitive parked the run (``sleep``/``wait_for_event``).

    Raised by :meth:`ReplayEngine.durable_sleep` / :meth:`durable_wait_for_event` on a
    miss with no resolving journal event. It unwinds the workflow coroutine so the run
    is released from memory with no live frame (ADR-0007); :meth:`ReplayEngine.drive`
    catches it, records no terminal event, and marks the run ``WAITING``. Subclasses
    ``BaseException`` so a user ``except Exception`` in a workflow body cannot swallow a
    durable park (mirroring ``asyncio.CancelledError``).
    """


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

        # -- durable-primitive (V3) replay state ---------------------------------
        #: Per-drive ordinals for durable primitives, keyed by a synthetic name
        #: (``sleep`` / ``event:<type>``). Kept separate from task ordinals: primitives
        #: append no ``TaskScheduled`` and are outside the task nondeterminism check.
        self._primitive_resolver = IdentityResolver()
        #: Timer identities that already have a recorded ``TimerCreated`` → kind.
        self._timers_created: dict[str, TimerKind] = {}
        #: Timer identities with a recorded ``TimerFired`` → kind (a resolved wait/sleep).
        self._timers_fired: dict[str, TimerKind] = {}
        #: Wait identities with a recorded ``EventWaitStarted`` (already parked once).
        self._waits_started: set[str] = set()
        #: Wait identities with a recorded ``ExternalEventReceived`` → encoded event ref.
        self._events_received: dict[str, Any] = {}

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
            elif event.type is EventType.TIMER_CREATED:
                self._timers_created[payload["identity"]] = TimerKind(payload["kind"])
            elif event.type is EventType.TIMER_FIRED:
                self._timers_fired[payload["identity"]] = TimerKind(payload["kind"])
            elif event.type is EventType.EVENT_WAIT_STARTED:
                self._waits_started.add(payload["identity"])
            elif event.type is EventType.EXTERNAL_EVENT_RECEIVED:
                self._events_received[payload["identity"]] = payload["event_ref"]

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

    # -- durable primitives (V3, N5) ---------------------------------------------

    async def durable_sleep(self, duration: timedelta) -> None:
        """Durable sleep: hit (``TimerFired``) returns; miss creates a timer and parks.

        On the first miss it appends ``TimerCreated`` (``fire_at = now + duration`` on
        the injected clock) plus a ``timers`` row and ``WorkflowWaiting``, then raises
        :class:`WorkflowParked` to release the run. On the resolving ``TimerFired`` the
        worker re-drives and this call is a journal hit that returns.
        """
        identity = f"sleep#{self._primitive_resolver.next('sleep').ordinal}"

        if identity in self._timers_fired:
            return None  # hit: the timer already fired — the sleep is over.

        if identity not in self._timers_created:
            fire_at = self._clock.now() + duration
            timer_id = uuid.uuid4().hex
            await self._commit(
                Event(
                    run_id=self._run_id,
                    type=EventType.TIMER_CREATED,
                    payload={
                        "timer_id": timer_id,
                        "kind": TimerKind.SLEEP.value,
                        "identity": identity,
                        "fire_at": fire_at.isoformat(),
                        "duration_seconds": duration.total_seconds(),
                    },
                    ts=self._clock.now(),
                )
            )
            await self._store.add_timer(
                TimerRecord(
                    timer_id=timer_id,
                    run_id=self._run_id,
                    kind=TimerKind.SLEEP,
                    identity=identity,
                    fire_at=fire_at,
                    status=TimerStatus.PENDING,
                    created_at=self._clock.now(),
                )
            )
            await self._commit(
                Event(
                    run_id=self._run_id,
                    type=EventType.WORKFLOW_WAITING,
                    payload={"reason": "sleep", "identity": identity},
                    ts=self._clock.now(),
                )
            )
        raise WorkflowParked

    async def durable_wait_for_event(
        self,
        event_type: str,
        key: str | None,
        timeout: timedelta | None,
        annotation: Any,
    ) -> Any:
        """Durable event wait: consume a matching inbox event or park until one arrives.

        Hit: an ``ExternalEventReceived`` returns the recorded event; a fired
        ``event_timeout`` returns ``None``. Miss: consume a buffered matching inbox
        event (append ``ExternalEventReceived``, return it) or, absent one, append
        ``EventWaitStarted`` (plus a ``event_timeout`` ``TimerCreated`` when a timeout is
        given) and park. Event wins over a simultaneously-due timeout (ADR-0021).
        """
        identity = f"event#{self._primitive_resolver.next(f'event:{event_type}').ordinal}"

        if identity in self._events_received:
            return rehydrate(self._events_received[identity], annotation)  # hit: delivered.
        if self._timers_fired.get(identity) is TimerKind.EVENT_TIMEOUT:
            return None  # hit: the wait timed out.

        # Miss: a buffered event delivered *before* the wait is matched from the inbox.
        match = await self._store.match_inbox_event(event_type, key)
        if match is not None:
            await self._store.consume_inbox_event(match.row_id)
            await self._commit(
                Event(
                    run_id=self._run_id,
                    type=EventType.EXTERNAL_EVENT_RECEIVED,
                    payload={
                        "identity": identity,
                        "event_type": event_type,
                        "key": key,
                        "event_ref": match.payload_ref,
                    },
                    ts=self._clock.now(),
                )
            )
            return rehydrate(match.payload_ref, annotation)

        # No matching event yet: start waiting (once) and park.
        if identity not in self._waits_started:
            timeout_timer_id: str | None = None
            if timeout is not None:
                timeout_timer_id = uuid.uuid4().hex
                fire_at = self._clock.now() + timeout
                await self._commit(
                    Event(
                        run_id=self._run_id,
                        type=EventType.TIMER_CREATED,
                        payload={
                            "timer_id": timeout_timer_id,
                            "kind": TimerKind.EVENT_TIMEOUT.value,
                            "identity": identity,
                            "fire_at": fire_at.isoformat(),
                            "duration_seconds": timeout.total_seconds(),
                        },
                        ts=self._clock.now(),
                    )
                )
                await self._store.add_timer(
                    TimerRecord(
                        timer_id=timeout_timer_id,
                        run_id=self._run_id,
                        kind=TimerKind.EVENT_TIMEOUT,
                        identity=identity,
                        fire_at=fire_at,
                        status=TimerStatus.PENDING,
                        created_at=self._clock.now(),
                    )
                )
            await self._commit(
                Event(
                    run_id=self._run_id,
                    type=EventType.EVENT_WAIT_STARTED,
                    payload={
                        "identity": identity,
                        "event_type": event_type,
                        "key": key,
                        "timeout_timer_id": timeout_timer_id,
                    },
                    ts=self._clock.now(),
                )
            )
            await self._commit(
                Event(
                    run_id=self._run_id,
                    type=EventType.WORKFLOW_WAITING,
                    payload={
                        "reason": "event",
                        "identity": identity,
                        "event_type": event_type,
                        "key": key,
                    },
                    ts=self._clock.now(),
                )
            )
        raise WorkflowParked

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
        except WorkflowParked:
            # A graceful durable park (sleep / wait_for_event): the run is released with
            # no terminal event and no WorkflowResumed — no ⚡ marker (ADR-0009/Q52). The
            # poll loop wakes it when the resolving timer fires or event arrives.
            await self._store.set_status(self._run_id, RunStatus.WAITING)
            return
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
