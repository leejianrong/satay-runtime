"""Timers and events (A5, N11) — the worker's poll loop and event inbox.

Persists timer rows and an event inbox, and runs the poll loop (~1s in dev) that fires
due timers and delivers events, resuming durably-parked runs by re-driving replay. An
asyncio background loop over the store, using the same **injected clock** as the
executor (ARCHITECTURE §3.5). Per tick it:

1. **delivers events first** — for every non-terminal run with an outstanding event
   wait, if a matching inbox event exists, re-drives the run (the wait consumes the
   event during replay). This runs before timeouts so a delivered event always wins a
   simultaneously-due timeout (ADR-0021);
2. **fires due timers** — a ``sleep`` timer appends ``TimerFired`` and re-drives; an
   ``event_timeout`` timer fires only if its wait has not already been resolved by an
   event, otherwise it is discarded (ADR-0021 event-wins).

Firing is **idempotent**: each fire is guarded by the timer row ``status`` and a
journal-presence check, so a timer fired twice does not double-resume. A re-drive
appends no ``WorkflowResumed`` and shows no ⚡ — a graceful wake, not a crash resume
(ADR-0009/Q52). Buffered matches are consumed FIFO by ``received_at`` (D22, ADR-0021).
"""

from __future__ import annotations

import inspect
from collections.abc import Sequence
from typing import Any, get_type_hints

from satay.api.registry import REGISTRY, WorkflowDefinition
from satay.config import EffectSafety
from satay.control.commands import CommandQueue, apply_command
from satay.journal import Store
from satay.journal.codec import rehydrate
from satay.journal.events import (
    TERMINAL_STATUSES,
    Event,
    EventType,
    TimerKind,
    TimerRecord,
    TimerStatus,
)
from satay.replay.engine import ReplayEngine
from satay.testing.clock import Clock, RealClock
from satay.testing.faults import FaultInjector
from satay.testing.rng import Rng


class TimerEventWorker:
    """The worker background loop that fires due timers and delivers inbox events (N11).

    Construct it over the same store and injected clock as the runs it wakes. Tests call
    :meth:`tick` directly after advancing the manual clock; ``satay dev`` calls
    :meth:`run` for a continuous background loop.
    """

    def __init__(
        self,
        *,
        store: Store,
        clock: Clock | None = None,
        rng: Rng | None = None,
        injector: FaultInjector | None = None,
        effect_safety: EffectSafety = EffectSafety.WARN,
        interval: float = 1.0,
        commands: CommandQueue | None = None,
    ) -> None:
        self._store = store
        self._clock = clock or RealClock()
        self._rng = rng
        self._injector = injector
        self._effect_safety = effect_safety
        self._interval = interval
        self._running = False
        #: The control-write command queue the worker drains each tick (V5, ADR-0012).
        #: ``None`` in a pure timer/event context (V1-V4 tests pass no queue).
        self._commands = commands

    # -- loop control ------------------------------------------------------------

    async def run(self) -> None:
        """Run the poll loop until :meth:`stop`, sleeping ``interval`` on the clock."""
        self._running = True
        while self._running:
            await self.tick()
            await self._clock.sleep(self._interval)

    def stop(self) -> None:
        """Ask the background :meth:`run` loop to exit after its current tick."""
        self._running = False

    async def tick(self) -> int:
        """Run one poll iteration; return the number of runs re-driven this tick.

        Applies queued control writes first (so an HTTP ``cancel``/``send_event`` lands
        before delivery), then delivers events before firing timers so a delivered event
        wins a simultaneously-due timeout (ADR-0021). The return count is the runs
        re-driven by event delivery + timer firing (V1-V4 semantics unchanged); a
        control write's effect is observed through those same paths.
        """
        await self._apply_commands()
        resumed = await self._deliver_events()
        resumed += await self._fire_timers()
        return resumed

    # -- control writes (V5, ADR-0012) -------------------------------------------

    async def _apply_commands(self) -> None:
        """Drain and apply queued control writes; the worker is the sole writer."""
        if self._commands is None:
            return
        for command in self._commands.drain():
            await apply_command(
                command,
                store=self._store,
                clock=self._clock,
                rng=self._rng,
                injector=self._injector,
                effect_safety=self._effect_safety,
            )

    # -- event delivery ----------------------------------------------------------

    async def _deliver_events(self) -> int:
        """Re-drive every non-terminal run whose outstanding wait has a matching event.

        The matching + consumption happens inside the re-drive's ``wait_for_event`` (one
        code path for both deliver-before-wait and deliver-after-park). Scanning by run
        rather than by run status keeps delivery correct even for a run that crashed
        after ``WorkflowWaiting`` but before its status became ``waiting``.
        """
        resumed = 0
        for run_id in await self._store.list_runs():
            record = await self._store.get_run(run_id)
            if record is None or record.status in TERMINAL_STATUSES:
                continue
            events = await self._store.read_events(run_id)
            for _identity, event_type, key in _outstanding_event_waits(events):
                if await self._store.match_inbox_event(event_type, key) is not None:
                    await self._redrive(run_id)
                    resumed += 1
                    break  # one wait resolves per re-drive; re-evaluate next tick.
        return resumed

    # -- timer firing ------------------------------------------------------------

    async def _fire_timers(self) -> int:
        """Fire due timers, then re-drive each affected (still non-terminal) run once."""
        due = await self._store.due_timers(self._clock.now())
        to_resume: set[str] = set()
        for timer in due:
            if await self._fire_one(timer):
                to_resume.add(timer.run_id)

        resumed = 0
        for run_id in to_resume:
            record = await self._store.get_run(run_id)
            if record is not None and record.status not in TERMINAL_STATUSES:
                await self._redrive(run_id)
                resumed += 1
        return resumed

    async def _fire_one(self, timer: TimerRecord) -> bool:
        """Fire one due timer idempotently; return whether the run should be re-driven.

        The idempotency guard is the timer ``status`` (only ``pending`` rows are due)
        plus a journal-presence check, so a duplicate fire appends no second
        ``TimerFired`` and does not double-resume.
        """
        events = await self._store.read_events(timer.run_id)

        if timer.kind is TimerKind.EVENT_TIMEOUT and _has_external_event_received(
            events, timer.identity
        ):
            # The wait was already resolved by a delivered event — discard the timeout.
            await self._store.set_timer_status(timer.timer_id, TimerStatus.DISCARDED)
            return False

        if _has_timer_fired(events, timer.identity, timer.kind):
            # Already fired (a duplicate due read): just settle the row, no re-resume.
            await self._store.set_timer_status(timer.timer_id, TimerStatus.FIRED)
            return False

        await self._store.append(
            Event(
                run_id=timer.run_id,
                type=EventType.TIMER_FIRED,
                payload={
                    "timer_id": timer.timer_id,
                    "kind": timer.kind.value,
                    "identity": timer.identity,
                    "fire_at": timer.fire_at.isoformat(),
                },
                ts=self._clock.now(),
            )
        )
        await self._store.set_timer_status(timer.timer_id, TimerStatus.FIRED)
        return True

    # -- re-drive ----------------------------------------------------------------

    async def _redrive(self, run_id: str) -> None:
        """Re-drive a parked run from its journal (a graceful wake — no WorkflowResumed)."""
        record = await self._store.get_run(run_id)
        if record is None or record.status in TERMINAL_STATUSES:
            return
        workflow_def = REGISTRY.get_workflow(record.workflow_name)
        if workflow_def is None:  # pragma: no cover - defensive: unregistered workflow
            return
        workflow_input = await self._reconstruct_input(run_id, workflow_def)
        engine = ReplayEngine(
            store=self._store,
            run_id=run_id,
            injector=self._injector,
            clock=self._clock,
            rng=self._rng,
            effect_safety=self._effect_safety,
        )
        await engine.drive(workflow_def, workflow_input)

    async def _reconstruct_input(self, run_id: str, workflow_def: WorkflowDefinition) -> Any:
        """Rehydrate the workflow input recorded on ``WorkflowCreated`` (crash survives)."""
        for event in await self._store.read_events(run_id):
            if event.type is EventType.WORKFLOW_CREATED:
                return rehydrate(event.payload.get("input_ref"), _input_annotation(workflow_def.fn))
        return None  # pragma: no cover - a run always has a WorkflowCreated head.


def _has_timer_fired(events: Sequence[Event], identity: str, kind: TimerKind) -> bool:
    return any(
        e.type is EventType.TIMER_FIRED
        and e.payload.get("identity") == identity
        and e.payload.get("kind") == kind.value
        for e in events
    )


def _has_external_event_received(events: Sequence[Event], identity: str) -> bool:
    return any(
        e.type is EventType.EXTERNAL_EVENT_RECEIVED and e.payload.get("identity") == identity
        for e in events
    )


def _outstanding_event_waits(events: Sequence[Event]) -> list[tuple[str, str, str | None]]:
    """Return ``(identity, event_type, key)`` for every unresolved ``EventWaitStarted``.

    A wait is resolved by a matching ``ExternalEventReceived`` or a fired
    ``event_timeout`` ``TimerFired`` on the same identity.
    """
    started: dict[str, tuple[str, str | None]] = {}
    resolved: set[str] = set()
    for event in events:
        identity = event.payload.get("identity")
        if not isinstance(identity, str):
            continue
        if event.type is EventType.EVENT_WAIT_STARTED:
            started[identity] = (event.payload["event_type"], event.payload.get("key"))
        elif event.type is EventType.EXTERNAL_EVENT_RECEIVED or (
            event.type is EventType.TIMER_FIRED
            and event.payload.get("kind") == TimerKind.EVENT_TIMEOUT.value
        ):
            resolved.add(identity)
    return [
        (identity, etype, key)
        for identity, (etype, key) in started.items()
        if identity not in resolved
    ]


def _input_annotation(fn: Any) -> Any:
    """Best-effort resolved annotation of ``fn``'s first parameter (``None`` if absent)."""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):  # pragma: no cover - builtins without signatures
        return None
    params = list(sig.parameters.values())
    if not params:
        return None
    try:
        hints = get_type_hints(fn)
    except Exception:
        ann = params[0].annotation
        return None if ann is inspect.Parameter.empty else ann
    return hints.get(params[0].name)


# Re-exported so callers can start the poll loop as ``from satay.timers import ...``.
__all__ = ["TimerEventWorker"]
