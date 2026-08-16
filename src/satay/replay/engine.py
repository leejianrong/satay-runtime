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

**Derived state is repaired, not re-created (ADR-0004).** Every durable write commits its
journal event *first* and derives side-table rows (``runs.status``, ``timers``) from it, so
a crash can leave the side table lagging the journal but never leading it. Resume therefore
guards each write on *its own* recorded presence and completes whichever writes are
missing. Concretely, a recorded ``TimerCreated`` whose ``timers`` row was lost to a crash
has that row rebuilt from the recorded payload — idempotently, keyed on the journal's
``timer_id`` — instead of the whole park being skipped as already done, which used to leave
a created timer with no row for the poll loop to fire and a run parked forever (KAN-443).

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
from collections.abc import Awaitable, Callable, Coroutine, Iterable, Mapping, Sequence
from contextvars import ContextVar
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, get_type_hints

from satay.api.registry import TaskDefinition, WorkflowDefinition
from satay.config import EffectSafety, NondeterminismPolicy, VersionMismatchPolicy
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
from satay.replay.failures import TaskFailedError
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

#: Whether the durable call about to run sits inside a ``return_exceptions=True``
#: composite (ADR-0027). Set at each composite boundary — ``durable_map`` and
#: ``durable_gather`` both assign their own mode before creating member tasks — monotonically,
#: so a fail-fast composite nested inside a collect one still records its failures, because
#: the enclosing collect composite is what makes them survivable. A ``ContextVar`` rather
#: than an engine attribute because fan-out members run as concurrent asyncio tasks,
#: which copy the context at creation, and two composites can be in flight at once.
_COLLECTING: ContextVar[bool] = ContextVar("satay_collecting", default=False)


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
        version_mismatch: VersionMismatchPolicy = VersionMismatchPolicy.WARN,
    ) -> None:
        self._store = store
        self._run_id = run_id
        self._injector = injector
        self._clock = clock or RealClock()
        self._rng = rng or SystemRng()
        self._effect_safety = effect_safety
        self._nondeterminism = nondeterminism
        #: Carried, not consulted here: the engine never resumes a run itself, but its
        #: child runs go through the resume path, and a child must not inherit a
        #: different default from its parent (ADR-0022/0023).
        self._version_mismatch = version_mismatch
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
        #: Identities with a recorded terminal ``TaskFailed`` → its payload (ADR-0027).
        #: The failure-side twin of ``self._completed``: a hit re-raises rather than
        #: re-executing, so a collected failure is once-recorded like a completion.
        self._failed: dict[CallIdentity, Mapping[str, Any]] = {}

        # -- durable-primitive (V3) replay state ---------------------------------
        #: Per-drive ordinals for durable primitives, keyed by a synthetic name
        #: (``sleep`` / ``event:<type>``). Kept separate from task ordinals: primitives
        #: append no ``TaskScheduled`` and are outside the task nondeterminism check.
        self._primitive_resolver = IdentityResolver()
        #: Timer identities with a recorded ``TimerCreated`` → the payload it recorded.
        #: The payload is kept whole (not just the kind) because it carries everything
        #: needed to rebuild the derived ``timers`` row on resume (KAN-443).
        self._timers_created: dict[str, dict[str, Any]] = {}
        #: Timer identities with a recorded ``TimerFired`` → kind (a resolved wait/sleep).
        self._timers_fired: dict[str, TimerKind] = {}
        #: Wait identities with a recorded ``EventWaitStarted`` (already parked once).
        self._waits_started: set[str] = set()
        #: Identities whose park is already recorded by a ``WorkflowWaiting``.
        self._waiting_recorded: set[str] = set()
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
            elif event.type is EventType.TASK_FAILED:
                identity = CallIdentity.from_payload(payload)
                self._failed[identity] = dict(payload)
            elif event.type is EventType.CHILD_WORKFLOW_SCHEDULED:
                identity = CallIdentity.from_payload(payload)
                self._children_scheduled[identity] = payload["child_run_id"]
            elif event.type is EventType.TIMER_CREATED:
                self._timers_created[payload["identity"]] = dict(payload)
            elif event.type is EventType.TIMER_FIRED:
                self._timers_fired[payload["identity"]] = TimerKind(payload["kind"])
            elif event.type is EventType.EVENT_WAIT_STARTED:
                self._waits_started.add(payload["identity"])
            elif event.type is EventType.WORKFLOW_WAITING:
                parked = payload.get("identity")
                if isinstance(parked, str):
                    self._waiting_recorded.add(parked)
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
        if identity in self._failed:
            # Hit on the failure side (ADR-0027): a recorded ``TaskFailed`` is terminal
            # for this logical call, so re-raise it instead of paying for the task again.
            raise _task_failure_error(self._failed[identity])

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
        return await self._execute(definition, identity, args, kwargs, key)

    async def _execute(
        self,
        definition: TaskDefinition,
        identity: CallIdentity,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        key: str,
    ) -> Any:
        """Hand a missed durable call to the executor, recording a *collected* failure.

        Under fail-fast (the default) this is a bare delegation and the executor's
        re-raise flows on to ``drive``, which records ``WorkflowFailed`` — unchanged.

        Inside a ``return_exceptions=True`` composite the run is going to *survive* this
        failure, so retry exhaustion has to become a durable fact of its own: append
        ``TaskFailed`` before letting the composite collect it (ADR-0027). Without that
        event the journal would hold a task with attempts and no terminal record, and any
        later resume would treat it as a miss and re-run a task that already spent its
        whole retry budget.
        """
        try:
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
        except Exception as exc:
            if isinstance(exc, _PROPAGATE) or not _COLLECTING.get():
                raise
            payload: dict[str, Any] = {
                **identity.payload_fields(),
                "error": {
                    "type": type(exc).__name__,
                    "message": str(exc),
                    "traceback": "".join(
                        traceback.format_exception(type(exc), exc, exc.__traceback__)
                    ),
                },
            }
            await self._commit(
                Event(
                    run_id=self._run_id,
                    type=EventType.TASK_FAILED,
                    payload=payload,
                    ts=self._clock.now(),
                )
            )
            self._failed[identity] = payload
            # Always the same type as the replay hit above, so a workflow that branches
            # on the collected error behaves identically on every pass (ADR-0027).
            raise _task_failure_error(payload) from exc

    # -- durable primitives (V3, N5) ---------------------------------------------

    async def _create_timer(
        self, *, kind: TimerKind, identity: str, duration: timedelta
    ) -> dict[str, Any]:
        """Record a new timer: append ``TimerCreated``, then insert its derived row.

        The journal event is committed **first** and the ``timers`` row is derived from
        it (ADR-0004). That ordering is deliberate and must not be inverted: the row may
        therefore *lag* the journal after a crash, but it can never *lead* it, so a
        recorded ``TimerCreated`` is always enough to rebuild the row. Returns the
        recorded payload so the caller can reference the ``timer_id`` it minted.
        """
        payload: dict[str, Any] = {
            "timer_id": uuid.uuid4().hex,
            "kind": kind.value,
            "identity": identity,
            "fire_at": (self._clock.now() + duration).isoformat(),
            "duration_seconds": duration.total_seconds(),
        }
        await self._commit(
            Event(
                run_id=self._run_id,
                type=EventType.TIMER_CREATED,
                payload=payload,
                ts=self._clock.now(),
            )
        )
        self._timers_created[identity] = payload
        await self._ensure_timer_row(payload)
        return payload

    async def _ensure_timer_row(self, recorded: dict[str, Any]) -> None:
        """Insert the ``timers`` row a recorded ``TimerCreated`` implies (idempotent).

        KAN-443: ``TimerCreated`` commits *before* its row, so a crash in that window
        left a timer the journal had created with **no row for the poll loop to fire** —
        and resume, seeing the recorded event, treated the timer as created, skipped the
        block, and re-parked. Nothing ever inserted the row and the run waited forever.

        The journal is the single source of truth and ``timers`` is derived from it
        (ADR-0004), which is the KAN-394 precedent — an idempotent repair on resume, not
        a transaction coupling the append to a side table. ``add_timer`` is
        ``INSERT OR IGNORE`` on ``timer_id``, and the ``timer_id`` comes from the journal
        rather than a fresh ``uuid4``, so repairing any number of times converges on
        exactly one row, and an already-``fired``/``discarded`` row is left settled.
        """
        await self._store.add_timer(
            TimerRecord(
                timer_id=recorded["timer_id"],
                run_id=self._run_id,
                kind=TimerKind(recorded["kind"]),
                identity=recorded["identity"],
                fire_at=datetime.fromisoformat(recorded["fire_at"]),
                status=TimerStatus.PENDING,
                created_at=self._clock.now(),
            )
        )

    async def durable_sleep(self, duration: timedelta) -> None:
        """Durable sleep: hit (``TimerFired``) returns; miss creates a timer and parks.

        On the first miss it appends ``TimerCreated`` (``fire_at = now + duration`` on
        the injected clock) plus a ``timers`` row and ``WorkflowWaiting``, then raises
        :class:`WorkflowParked` to release the run. On the resolving ``TimerFired`` the
        worker re-drives and this call is a journal hit that returns.

        Each write is guarded by *its own* recorded presence in the journal, so a resume
        after a crash part-way through completes whichever writes are missing rather than
        skipping the lot: an already-recorded ``TimerCreated`` has its derived row
        repaired (KAN-443) at the ``fire_at`` the journal recorded, so the sleep keeps its
        original deadline instead of silently restarting from resume time.
        """
        identity = f"sleep#{self._primitive_resolver.next('sleep').ordinal}"

        if identity in self._timers_fired:
            return None  # hit: the timer already fired — the sleep is over.

        recorded = self._timers_created.get(identity)
        if recorded is None:
            await self._create_timer(kind=TimerKind.SLEEP, identity=identity, duration=duration)
        else:
            await self._ensure_timer_row(recorded)

        if identity not in self._waiting_recorded:
            await self._commit(
                Event(
                    run_id=self._run_id,
                    type=EventType.WORKFLOW_WAITING,
                    payload={"reason": "sleep", "identity": identity},
                    ts=self._clock.now(),
                )
            )
            self._waiting_recorded.add(identity)
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
        #
        # The timeout timer is guarded by its *own* recorded ``TimerCreated``, not by the
        # ``EventWaitStarted`` that commits after it (KAN-443). Under the old shared guard
        # a crash between the two re-entered this block on resume and minted a *second*
        # timeout timer: a duplicate ``TimerCreated`` for one wait (ADR-0004) whose
        # deadline had slid forward to resume time, quietly extending the timeout the
        # author asked for (ADR-0021).
        timeout_timer_id: str | None = None
        if timeout is not None:
            recorded_timeout = self._timers_created.get(identity)
            if recorded_timeout is None:
                recorded_timeout = await self._create_timer(
                    kind=TimerKind.EVENT_TIMEOUT, identity=identity, duration=timeout
                )
            else:
                await self._ensure_timer_row(recorded_timeout)
            timeout_timer_id = recorded_timeout["timer_id"]

        if identity not in self._waits_started:
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
            self._waits_started.add(identity)

        if identity not in self._waiting_recorded:
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
            self._waiting_recorded.add(identity)
        raise WorkflowParked

    # -- composite primitives (V4, N5/A6) ---------------------------------------

    async def durable_map(
        self,
        definition: TaskDefinition,
        items: Iterable[Any],
        key_fn: Callable[[Any], str] | None,
        concurrency: int,
        return_exceptions: bool = False,
    ) -> list[Any]:
        """Durable fan-out of ``definition`` over ``items``, keyed by ``key_fn`` (A6.1).

        Each item is a keyed durable call ``(task_name, key)`` that independently
        consults the journal — a recorded completion is reused, a miss executes — so on
        resume mid-fan-out only unresolved items re-run (design rule 2). Up to
        ``concurrency`` items run at once on the asyncio loop (a bounded semaphore), and
        results rejoin in **input order** regardless of completion order.

        Fail-fast by default (ADR-0020): a failed item raises through the ``map`` and
        in-flight siblings settle with their results discarded. With
        ``return_exceptions=True`` (collect mode, ADR-0027) every item is allowed to
        settle, each failure is recorded as its own terminal ``TaskFailed``, and the
        failing slots hold a :class:`~satay.replay.failures.TaskFailedError`.
        """
        if concurrency < 1:
            raise ValueError("satay.map concurrency= must be >= 1")
        pairs = resolve_map_keys(items, key_fn)  # validates key presence + uniqueness
        group = f"map:{self._map_resolver.next(definition.name).ordinal}:{definition.name}"
        semaphore = asyncio.Semaphore(concurrency)
        #: Set when an item dies from a worker crash: a dead worker starts no new items,
        #: so queued items still behind the semaphore must not begin (their results would
        #: never be reachable anyway — the composite is about to raise the crash). Note
        #: an *ordinary* item failure never sets this, in either mode.
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

        tasks = self._spawn_members(
            [run_item(item, key) for item, key in pairs], collecting=return_exceptions
        )
        return await self._settle_composite(tasks, return_exceptions=return_exceptions)

    async def durable_gather(
        self, awaitables: Sequence[Awaitable[Any]], return_exceptions: bool = False
    ) -> list[Any]:
        """Await heterogeneous durable calls together, rejoining **positionally** (A6.1).

        Members are ordinary durable awaitables — a task call, a nested ``map``, or a
        ``start_child`` (whose returned handle is transparently resolved to the child's
        result). Each keeps its own identity; results rejoin in argument order.

        Fail-fast by default (ADR-0020): one failing member fails the whole ``gather``.
        With ``return_exceptions=True`` (ADR-0027) every member settles and a failing
        slot holds the deterministic error its member raised — a ``TaskFailedError`` for
        a task, a ``WorkflowFailedError`` for a child run.
        """
        tasks = self._spawn_members(
            [self._resolve_member(a) for a in awaitables], collecting=return_exceptions
        )
        return await self._settle_composite(tasks, return_exceptions=return_exceptions)

    @staticmethod
    def _spawn_members(
        coros: list[Coroutine[Any, Any, Any]], *, collecting: bool
    ) -> list[asyncio.Task[Any]]:
        """Schedule fan-out members with this composite's collect mode bound to each.

        ``asyncio.Task`` copies the current context at creation, so setting ``_COLLECTING``
        around the spawn — and resetting it straight after — hands every member the mode
        of *this* composite without leaking it to whatever the workflow does next.

        The flag is **monotone**: a fail-fast composite nested inside a collect one stays
        collecting, because the question the flag actually answers is not "which mode did
        this call site ask for" but "is the run going to survive this failure". If some
        enclosing composite is going to catch it, the failure is survivable and must
        become a durable ``TaskFailed`` — otherwise a resume would re-run and re-pay for a
        task that already spent its retry budget (ADR-0027). The inner composite still
        *behaves* fail-fast: it raises, and its siblings' results are still discarded.
        """
        token = _COLLECTING.set(collecting or _COLLECTING.get())
        try:
            return [asyncio.create_task(coro) for coro in coros]
        finally:
            _COLLECTING.reset(token)

    async def _resolve_member(self, awaitable: Awaitable[Any]) -> Any:
        """Await a ``gather`` member; coerce a child :class:`RunHandle` to its result."""
        from satay.api.run_handle import RunHandle

        value = await awaitable
        if isinstance(value, RunHandle):
            return await value.result()
        return value

    async def _settle_composite(
        self, tasks: list[asyncio.Task[Any]], *, return_exceptions: bool = False
    ) -> list[Any]:
        """Settle fan-out tasks; a crash always cancels in-flight siblings.

        Fail-fast (the default, ADR-0020) waits for the first exception, then: a
        :class:`SimulatedCrash` (or dev-time divergence) models worker death — cancel the
        in-flight siblings so they record nothing more and propagate the crash; an
        ordinary task failure lets siblings settle (results discarded) then raises the
        **first failure in input order**.

        Collect mode (ADR-0027) waits for *every* member instead and returns results and
        errors positionally. A crash is still not a member outcome: it aborts the
        composite exactly as above, because a dead worker cannot honestly report on the
        siblings it never finished.
        """
        if not tasks:
            return []
        if return_exceptions:
            await self._settle_all(tasks)
        else:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)

        crash = self._first_exception(tasks, propagate_only=True)
        if crash is not None:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise crash

        if return_exceptions:
            return [_settled_outcome(task) for task in tasks]

        if self._first_exception(tasks) is not None:
            # An ordinary member failed: let in-flight siblings settle, discard their
            # results, then raise the first failure in input order (deterministic).
            await asyncio.gather(*tasks, return_exceptions=True)
            failure = self._first_exception(tasks)
            assert failure is not None
            raise failure

        return [task.result() for task in tasks]

    async def _settle_all(self, tasks: list[asyncio.Task[Any]]) -> None:
        """Wait for every member, returning early only when a member hits a crash.

        ``FIRST_EXCEPTION`` fires on an ordinary collected failure too, so this loops
        until nothing is pending — but it re-checks for an out-of-band crash after each
        wake, so a :class:`SimulatedCrash` still aborts the composite promptly instead of
        waiting on siblings a dead worker will never finish.
        """
        pending = set(tasks)
        while pending:
            _, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_EXCEPTION)
            if self._first_exception(tasks, propagate_only=True) is not None:
                return

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
        if identity in self._failed:
            raise _task_failure_error(self._failed[identity])  # recorded terminal failure

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
        return await self._execute(definition, identity, (item,), {}, idem)

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
            version_mismatch=self._version_mismatch,
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
            version_mismatch=self._version_mismatch,
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


def _settled_outcome(task: asyncio.Task[Any]) -> Any:
    """One collect-mode slot: the member's result, or the error it raised (ADR-0027)."""
    if task.cancelled():  # pragma: no cover - only reachable on the crash path, which raises
        return asyncio.CancelledError()
    exception = task.exception()
    return task.result() if exception is None else exception


def _task_failure_error(payload: Mapping[str, Any]) -> TaskFailedError:
    """Build the :class:`TaskFailedError` a recorded ``TaskFailed`` payload describes.

    The single constructor for a collected task failure — used both when the failure is
    first recorded and when replay reads it back — so the exception a workflow sees is
    identical on every pass (ADR-0027).
    """
    error = payload.get("error", {})
    ordinal = payload.get("ordinal")
    return TaskFailedError(
        payload["task_name"],
        error.get("type", "Exception"),
        error.get("message", ""),
        error.get("traceback", ""),
        key=payload.get("key"),
        ordinal=None if ordinal is None else int(ordinal),
    )


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
