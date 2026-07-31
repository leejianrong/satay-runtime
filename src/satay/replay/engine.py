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

The terminal append is **idempotent**: a journal that already carries a terminal event
short-circuits the drive (the workflow is not re-run and no second terminal event is
appended), because the terminal event commits *before* the run status flips and a crash
in that window would otherwise duplicate it (ADR-0004).

**Nondeterminism (N9).** If a durable call's task name does not match the journal
entry at that global position, the engine raises :class:`NondeterminismError`
(expected-vs-actual). Policy follows the **nondeterminism policy** (ADR-0003/0022),
which is separate from ``effect_safety`` and defaults to ``strict``: ``strict`` fails,
``warn`` logs and lets the divergent call proceed as a fresh miss (so the run can
complete with a wrong result), ``off`` does the same silently.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import traceback
import uuid
from collections.abc import Awaitable, Callable, Iterable, Sequence
from datetime import timedelta
from typing import TYPE_CHECKING, Any, get_type_hints

from satay.api.registry import TaskDefinition, WorkflowDefinition
from satay.config import EffectSafety, NondeterminismPolicy
from satay.executor import LocalTaskExecutor, TaskExecutor
from satay.journal import Store
from satay.journal.codec import encode, rehydrate
from satay.journal.events import (
    TERMINAL_STATUSES,
    Event,
    EventType,
    RunRecord,
    RunStatus,
    TimerKind,
    TimerRecord,
    TimerStatus,
)
from satay.replay.driver import CURRENT_DRIVER
from satay.replay.identity import (
    CallIdentity,
    IdentityResolver,
    idempotency_key,
    resolve_map_keys,
)
from satay.replay.nondeterminism import EffectSafetyError, NondeterminismError
from satay.testing.clock import Clock, RealClock
from satay.testing.faults import FaultInjector, SimulatedCrash
from satay.testing.rng import Rng, SystemRng

if TYPE_CHECKING:
    from satay.api.run_handle import RunHandle

#: Default in-flight bound for ``satay.map`` when ``concurrency=`` is unspecified.
DEFAULT_MAP_CONCURRENCY = 8

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

#: Terminal journal event → the run status it records. The journal is the source of
#: truth for terminality (ADR-0004); ``runs.status`` is a denormalisation of it.
_TERMINAL_EVENT_STATUS: dict[EventType, RunStatus] = {
    EventType.WORKFLOW_COMPLETED: RunStatus.COMPLETED,
    EventType.WORKFLOW_FAILED: RunStatus.FAILED,
    EventType.WORKFLOW_CANCELLED: RunStatus.CANCELLED,
}


def _recorded_terminal_status(events: Sequence[Event]) -> RunStatus | None:
    """The terminal status already recorded in this journal, or ``None`` if non-terminal."""
    for event in reversed(events):
        status = _TERMINAL_EVENT_STATUS.get(event.type)
        if status is not None:
            return status
    return None


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
        nondeterminism: NondeterminismPolicy = NondeterminismPolicy.STRICT,
    ) -> None:
        self._store = store
        self._run_id = run_id
        self._injector = injector
        self._clock = clock or RealClock()
        self._rng = rng or SystemRng()
        self._effect_safety = effect_safety
        self._nondeterminism = nondeterminism
        self._executor = executor or LocalTaskExecutor(
            self._commit, clock=self._clock, rng=self._rng
        )

        self._resolver = IdentityResolver()
        #: Ordinals for child-workflow calls (``satay.start_child``), kept separate from
        #: task ordinals: a child appends ``ChildWorkflowScheduled`` (not ``TaskScheduled``)
        #: and is outside the task nondeterminism position check.
        self._child_resolver = IdentityResolver()
        #: Per-drive ordinals for ``satay.map`` call sites, used only to group a map's
        #: items in the journal for the V6 tree (keyed items carry their own identity).
        self._map_resolver = IdentityResolver()
        self._call_index = 0
        self._completed: dict[CallIdentity, Any] = {}
        self._scheduled: set[CallIdentity] = set()
        self._schedule_order: list[str] = []
        #: Recorded child runs by their originating call identity → child ``run_id``.
        self._children_scheduled: dict[CallIdentity, str] = {}
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
                identity = CallIdentity.from_payload(payload)
                self._scheduled.add(identity)
                # Only ordinal (non-keyed) calls take a slot in the nondeterminism
                # position list; keyed map items resolve independently of the ordinal.
                if not identity.is_keyed:
                    self._schedule_order.append(payload["task_name"])
            elif event.type is EventType.TASK_ATTEMPT_STARTED:
                identity = CallIdentity.from_payload(payload)
                attempt = int(payload.get("attempt", 1))
                self._max_attempt[identity] = max(self._max_attempt.get(identity, 0), attempt)
            elif event.type is EventType.TASK_ATTEMPT_FAILED:
                identity = CallIdentity.from_payload(payload)
                attempt = int(payload.get("attempt", 1))
                self._max_attempt[identity] = max(self._max_attempt.get(identity, 0), attempt)
                self._failures[identity] = self._failures.get(identity, 0) + 1
            elif event.type is EventType.TASK_COMPLETED:
                identity = CallIdentity.from_payload(payload)
                self._completed[identity] = payload["output_ref"]
            elif event.type is EventType.CHILD_WORKFLOW_SCHEDULED:
                identity = CallIdentity.from_payload(payload)
                self._children_scheduled[identity] = payload["child_run_id"]
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

        key = idempotency_key(self._run_id, identity.task_name, identity.key_component)
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

    # -- composite primitives (V4, N5/A6) ---------------------------------------

    async def durable_map(
        self,
        definition: TaskDefinition,
        items: Iterable[Any],
        key_fn: Callable[[Any], str] | None,
        concurrency: int,
    ) -> list[Any]:
        """Durable fan-out of ``definition`` over ``items``, keyed by ``key_fn`` (A6.1).

        Each item is a keyed durable call ``(task_name, key)`` that independently
        consults the journal — a recorded completion is reused, a miss executes — so on
        resume mid-fan-out only unresolved items re-run (design rule 2). Up to
        ``concurrency`` items run at once on the asyncio loop (a bounded semaphore), and
        results rejoin in **input order** regardless of completion order. Fail-fast per
        ADR-0020: a failed item raises through the ``map`` and in-flight siblings settle
        with their results discarded.
        """
        if concurrency < 1:
            raise ValueError("satay.map concurrency= must be >= 1")
        pairs = resolve_map_keys(items, key_fn)  # validates key presence + uniqueness
        group = f"map:{self._map_resolver.next(definition.name).ordinal}:{definition.name}"
        semaphore = asyncio.Semaphore(concurrency)
        #: Set when an item dies from a worker crash: a dead worker starts no new items,
        #: so queued items still behind the semaphore must not begin (their results would
        #: never be reachable anyway — the composite is about to raise the crash).
        aborted = asyncio.Event()

        async def run_item(item: Any, key: str) -> Any:
            async with semaphore:
                if aborted.is_set():
                    return None  # worker already dead — do not start new work.
                try:
                    return await self._keyed_call(definition, item, key, group)
                except _PROPAGATE:
                    aborted.set()
                    raise

        tasks = [asyncio.create_task(run_item(item, key)) for item, key in pairs]
        return await self._settle_composite(tasks)

    async def durable_gather(self, awaitables: Sequence[Awaitable[Any]]) -> list[Any]:
        """Await heterogeneous durable calls together, rejoining **positionally** (A6.1).

        Members are ordinary durable awaitables — a task call, a nested ``map``, or a
        ``start_child`` (whose returned handle is transparently resolved to the child's
        result). Each keeps its own identity; results rejoin in argument order. Fail-fast
        per ADR-0020: one failing member fails the whole ``gather``.
        """
        tasks = [asyncio.create_task(self._resolve_member(a)) for a in awaitables]
        return await self._settle_composite(tasks)

    async def _resolve_member(self, awaitable: Awaitable[Any]) -> Any:
        """Await a ``gather`` member; coerce a child :class:`RunHandle` to its result."""
        from satay.api.run_handle import RunHandle

        value = await awaitable
        if isinstance(value, RunHandle):
            return await value.result()
        return value

    async def _settle_composite(self, tasks: list[asyncio.Task[Any]]) -> list[Any]:
        """Await fan-out tasks fail-fast (ADR-0020); a crash cancels in-flight siblings.

        Waits for the first exception, then: a :class:`SimulatedCrash` (or dev-time
        divergence) models worker death — cancel the in-flight siblings so they record
        nothing more and propagate the crash; an ordinary task failure lets siblings
        settle (results discarded) then raises the **first failure in input order**.
        """
        if not tasks:
            return []
        await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)

        crash = self._first_exception(tasks, propagate_only=True)
        if crash is not None:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise crash

        if self._first_exception(tasks) is not None:
            # An ordinary member failed: let in-flight siblings settle, discard their
            # results, then raise the first failure in input order (deterministic).
            await asyncio.gather(*tasks, return_exceptions=True)
            failure = self._first_exception(tasks)
            assert failure is not None
            raise failure

        return [task.result() for task in tasks]

    @staticmethod
    def _first_exception(
        tasks: list[asyncio.Task[Any]], *, propagate_only: bool = False
    ) -> BaseException | None:
        """The first exception in input order among finished tasks (crashes if asked)."""
        for task in tasks:
            if task.done() and not task.cancelled():
                exc = task.exception()
                if exc is not None and (not propagate_only or isinstance(exc, _PROPAGATE)):
                    return exc
        return None

    async def _keyed_call(self, definition: TaskDefinition, item: Any, key: str, group: str) -> Any:
        """One keyed ``map`` item: reuse a recorded result or schedule + execute it."""
        identity = CallIdentity(task_name=definition.name, key=key)

        if identity in self._completed:
            return rehydrate(self._completed[identity], _return_annotation(definition.fn))

        self._enforce_effect_safety(definition)
        if identity not in self._scheduled:
            await self._commit(
                Event(
                    run_id=self._run_id,
                    type=EventType.TASK_SCHEDULED,
                    payload={
                        **identity.payload_fields(),
                        "input_ref": encode([item]),
                        "map_group": group,
                    },
                    ts=self._clock.now(),
                )
            )
            self._scheduled.add(identity)

        idem = idempotency_key(self._run_id, identity.task_name, identity.key_component)
        return await self._executor.execute(
            run_id=self._run_id,
            definition=definition,
            identity=identity,
            args=(item,),
            kwargs={},
            key=idem,
            prior_attempts=self._max_attempt.get(identity, 0),
            prior_failures=self._failures.get(identity, 0),
        )

    async def durable_child(
        self,
        workflow_def: WorkflowDefinition,
        workflow_input: Any,
        key: str | None,
    ) -> RunHandle:
        """Start (or reuse) a linked child run and return its handle (A6.2, design rule 3).

        The child call is a durable call on the parent: on the first miss the parent
        records ``ChildWorkflowScheduled`` with the child ``run_id`` and this call's
        identity, and the child's ``WorkflowCreated`` records the reverse ``parent_run_id``
        + originating identity (so the V6 tree is recoverable both ways). The child is a
        full run with its own journal, driven to a terminal state here; a child crashed
        mid-flight is **resumed** (not restarted) on parent replay, and an
        already-completed child is reused. A failed child surfaces as a raised exception
        (fail-fast, ADR-0020), re-raised deterministically from the child journal on replay.
        """
        if key is not None:
            identity = CallIdentity(task_name=f"child:{workflow_def.name}", key=key)
        else:
            identity = self._child_resolver.next(f"child:{workflow_def.name}")

        child_run_id = self._children_scheduled.get(identity)
        if child_run_id is None:
            child_run_id = uuid.uuid4().hex
            await self._commit(
                Event(
                    run_id=self._run_id,
                    type=EventType.CHILD_WORKFLOW_SCHEDULED,
                    payload={
                        **identity.payload_fields(),
                        "child_run_id": child_run_id,
                        "workflow_name": workflow_def.name,
                        "input_ref": encode(workflow_input),
                    },
                    ts=self._clock.now(),
                )
            )
            self._children_scheduled[identity] = child_run_id

        await self._drive_child(workflow_def, child_run_id, workflow_input, identity)

        record = await self._store.get_run(child_run_id)
        if record is not None and record.status is RunStatus.FAILED:
            # Fail-fast: surface the child's recorded failure as a raised exception.
            raise _child_failure_error(await self._store.read_events(child_run_id))

        return self._build_child_handle(workflow_def, child_run_id, workflow_input)

    async def _drive_child(
        self,
        workflow_def: WorkflowDefinition,
        child_run_id: str,
        workflow_input: Any,
        parent_identity: CallIdentity,
    ) -> None:
        """Create/resume the child run and drive it to a terminal (or waiting) state."""
        from satay.versioning import stamp_code_version

        record = await self._store.get_run(child_run_id)
        if record is None:
            code_version = stamp_code_version()
            await self._store.create_run(
                RunRecord(
                    run_id=child_run_id,
                    workflow_name=workflow_def.name,
                    status=RunStatus.RUNNING,
                    code_version=code_version,
                    created_at=self._clock.now(),
                )
            )
            await self._commit(
                Event(
                    run_id=child_run_id,
                    type=EventType.WORKFLOW_CREATED,
                    payload={
                        "workflow_name": workflow_def.name,
                        "input_ref": encode(workflow_input),
                        "code_version": code_version,
                        "parent_run_id": self._run_id,
                        "parent_call": parent_identity.payload_fields(),
                    },
                    ts=self._clock.now(),
                )
            )
        elif record.status in TERMINAL_STATUSES:
            return  # already terminal: reuse (completed) or surface (failed) upstream.
        elif record.status is not RunStatus.WAITING:
            # Non-terminal and not durably parked → crashed mid-flight: resume (⚡).
            await self._commit(
                Event(run_id=child_run_id, type=EventType.WORKFLOW_RESUMED, ts=self._clock.now())
            )

        child_engine = ReplayEngine(
            store=self._store,
            run_id=child_run_id,
            injector=self._injector,
            clock=self._clock,
            rng=self._rng,
            effect_safety=self._effect_safety,
            nondeterminism=self._nondeterminism,
        )
        await child_engine.drive(workflow_def, workflow_input)

    def _build_child_handle(
        self, workflow_def: WorkflowDefinition, child_run_id: str, workflow_input: Any
    ) -> RunHandle:
        """A handle to the (now terminal) child run, for the parent to read its result."""
        from satay.api.runner import build_run_handle

        return build_run_handle(
            workflow_def.fn,
            workflow_input,
            run_id=child_run_id,
            idempotency_key=None,
            store=self._store,
            injector=self._injector,
            clock=self._clock,
            rng=self._rng,
            effect_safety=self._effect_safety,
            nondeterminism=self._nondeterminism,
        )

    # -- policy ------------------------------------------------------------------

    def _on_nondeterminism(self, position: int, *, expected: str, actual: str) -> None:
        """Apply the nondeterminism policy to a replay divergence (N9, ADR-0022).

        Consults the dedicated nondeterminism policy, **not** ``effect_safety``: strict
        (the default) raises, warn logs, off is silent. Under warn/off the divergent call
        falls through as a fresh miss and the run can complete with a wrong result.
        """
        error = NondeterminismError(position=position, expected=expected, actual=actual)
        if self._nondeterminism is NondeterminismPolicy.STRICT:
            raise error
        if self._nondeterminism is NondeterminismPolicy.WARN:
            # Opt-in dev mode: warn; the developer recovers by forking the run at a
            # chosen point. The divergent call still proceeds as a fresh miss below.
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

        # Idempotent terminal append (ADR-0004). The terminal event is committed before
        # the run's status flips, so a crash in that window leaves a journal that is
        # already terminal on a run that still reads non-terminal. Re-running the
        # workflow here would append a SECOND terminal event; the journal is the source
        # of truth, so short-circuit and only reconcile the denormalised status.
        recorded = _recorded_terminal_status(events)
        if recorded is not None:
            await self._store.set_status(self._run_id, recorded)
            return

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


def _child_failure_error(events: Sequence[Event]) -> Exception:
    """Build the exception a failed child surfaces to its parent (from the child journal).

    Reconstructs the recorded ``WorkflowFailed`` as a :class:`WorkflowFailedError`, so the
    parent raises it natively (fail-fast, ADR-0020) and it re-raises identically from the
    journal on every parent replay.
    """
    from satay.api.run_handle import WorkflowFailedError

    for event in reversed(events):
        if event.type is EventType.WORKFLOW_FAILED:
            error = event.payload["error"]
            return WorkflowFailedError(error["type"], error["message"], error["traceback"])
    return RuntimeError("child workflow failed without a recorded error")  # pragma: no cover


def _return_annotation(fn: Any) -> Any:
    """Best-effort resolved return annotation of ``fn`` (``None`` if absent/unresolvable)."""
    try:
        hints = get_type_hints(fn)
    except Exception:
        sig = inspect.signature(fn)
        ann = sig.return_annotation
        return None if ann is inspect.Signature.empty else ann
    return hints.get("return")
