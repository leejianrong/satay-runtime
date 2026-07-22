"""The replay engine (N6) — re-runs a workflow and reconciles it with the journal.

Given a ``run_id``, the engine loads the ordered journal, then re-runs the workflow
coroutine top-to-bottom. On each awaited durable call it resolves identity
(``(task_name, ordinal)``, N7) and consults the journal:

- **hit** — a ``TaskCompleted`` exists for this identity: return the rehydrated
  recorded result *without executing* the task.
- **miss** — append ``TaskScheduled`` (unless already recorded, e.g. a mid-task
  crash) and invoke the executor, continuing with the fresh result.

On workflow return it appends ``WorkflowCompleted``; on a native workflow/task error
it appends ``WorkflowFailed`` with the traceback. Every append goes through
:meth:`_commit`, which fires the fault injector post-commit (ADR-0011) — a
``SimulatedCrash`` is allowed to propagate so it models a real worker death.

The **lightweight V1 determinism guard**: if the durable call at a global position
has a *different* task name than the one recorded at that position, raise a plain
error. Full ``NondeterminismError`` semantics are V2 — this only prevents a silent
mis-resume.
"""

from __future__ import annotations

import inspect
import traceback
from typing import Any, get_type_hints

from satay.api.registry import TaskDefinition, WorkflowDefinition
from satay.executor import LocalTaskExecutor, TaskExecutor
from satay.journal import Store
from satay.journal.codec import encode, rehydrate
from satay.journal.events import Event, EventType, RunStatus
from satay.replay.driver import CURRENT_DRIVER
from satay.replay.identity import CallIdentity, IdentityResolver
from satay.testing.clock import Clock, RealClock
from satay.testing.faults import FaultInjector


class ReplayEngine:
    """Drives one run: replays recorded durable calls, executes the misses."""

    def __init__(
        self,
        *,
        store: Store,
        run_id: str,
        injector: FaultInjector | None = None,
        clock: Clock | None = None,
        executor: TaskExecutor | None = None,
    ) -> None:
        self._store = store
        self._run_id = run_id
        self._injector = injector
        self._clock = clock or RealClock()
        self._executor = executor or LocalTaskExecutor(self._commit, clock=self._clock)

        self._resolver = IdentityResolver()
        self._call_index = 0
        self._completed: dict[CallIdentity, Any] = {}
        self._scheduled: set[CallIdentity] = set()
        self._schedule_order: list[str] = []

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

        # V1 determinism guard: compare the task name against what was recorded at
        # this global durable-call position.
        position = self._call_index
        self._call_index += 1
        if position < len(self._schedule_order):
            recorded_name = self._schedule_order[position]
            if recorded_name != definition.name:
                raise RuntimeError(
                    f"nondeterministic replay at durable-call position {position}: "
                    f"journal recorded task {recorded_name!r} but replay issued "
                    f"{definition.name!r} (the workflow changed between runs)"
                )

        if identity in self._completed:
            # Hit: rehydrate the recorded result; do NOT execute the task.
            return rehydrate(self._completed[identity], _return_annotation(definition.fn))

        # Miss: record the schedule (once) and execute the task.
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

        return await self._executor.execute(
            run_id=self._run_id,
            definition=definition,
            identity=identity,
            args=args,
            kwargs=kwargs,
        )

    # -- drive -------------------------------------------------------------------

    async def drive(self, workflow_def: WorkflowDefinition, workflow_input: Any) -> None:
        """Re-run the workflow to a terminal state, reconciling with the journal."""
        events = list(await self._store.read_events(self._run_id))
        self._load_journal(events)

        token = CURRENT_DRIVER.set(self)
        try:
            result = await workflow_def.fn(workflow_input)
        except Exception as exc:
            # A SimulatedCrash models a worker death: let it propagate unrecorded.
            from satay.testing.faults import SimulatedCrash

            if isinstance(exc, SimulatedCrash):
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
