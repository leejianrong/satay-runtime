"""Run orchestration: create / resume / no-op, then drive the replay engine (N3).

This module owns the ``satay.start`` control flow. It is imported *lazily* by
:func:`satay.api.primitives.start` (never by ``satay.api.__init__``) so it can freely
import the replay engine, store, and executor without a package import cycle.

The V1 resume mechanism is **by ``run_id``** (keyed idempotent look-up, N13, is V2):

- **New run** (unknown ``run_id``): stamp the code version, insert the run row,
  append ``WorkflowCreated``, and drive.
- **Resume** (known, non-terminal ``run_id``): append ``WorkflowResumed`` — the event
  the ⚡ interruption marker is computed from (ADR-0009/Q52) — and re-drive.
- **Terminal no-op** (known, terminal ``run_id``): return the recorded outcome without
  re-driving.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from satay.api.registry import WorkflowDefinition
from satay.api.run_handle import RunHandle, WorkflowFailedError
from satay.journal import Store
from satay.journal.codec import encode, rehydrate
from satay.journal.events import (
    TERMINAL_STATUSES,
    Event,
    EventType,
    RunRecord,
    RunStatus,
    utc_now,
)
from satay.replay.engine import ReplayEngine, _return_annotation
from satay.testing.clock import Clock
from satay.testing.faults import FaultInjector
from satay.versioning import stamp_code_version


class RunController:
    """Backs a :class:`RunHandle`: performs create/resume/no-op and drives the engine."""

    def __init__(
        self,
        *,
        store: Store,
        run_id: str,
        workflow_def: WorkflowDefinition,
        workflow_input: Any,
        idempotency_key: str | None,
        injector: FaultInjector | None,
        clock: Clock | None,
    ) -> None:
        self._store = store
        self._run_id = run_id
        self._wf = workflow_def
        self._input = workflow_input
        self._idempotency_key = idempotency_key
        self._injector = injector
        self._clock = clock

    def _now(self) -> Any:
        return self._clock.now() if self._clock is not None else utc_now()

    async def _commit(self, event: Event) -> Event:
        stored = await self._store.append(event)
        if self._injector is not None:
            await self._injector.reached(stored.type.value)
        return stored

    async def result(self) -> Any:
        """Ensure the run exists, drive it if non-terminal, return/raise the outcome."""
        record = await self._store.get_run(self._run_id)

        if record is None:
            await self._create()
            await self._drive()
        elif record.status in TERMINAL_STATUSES:
            # Terminal no-op: return the recorded outcome without re-driving.
            pass
        else:
            # Resume a run interrupted mid-execution (not durably parked): the
            # WorkflowResumed event is what renders the ⚡ marker (ADR-0009/Q52).
            await self._commit(
                Event(run_id=self._run_id, type=EventType.WORKFLOW_RESUMED, ts=self._now())
            )
            await self._drive()

        return await self._outcome()

    async def status(self) -> str:
        """Return the run's current status ('running' until the row exists)."""
        record = await self._store.get_run(self._run_id)
        if record is None:
            return RunStatus.RUNNING.value
        return record.status.value

    # -- internals ---------------------------------------------------------------

    async def _create(self) -> None:
        code_version = stamp_code_version()
        await self._store.create_run(
            RunRecord(
                run_id=self._run_id,
                workflow_name=self._wf.name,
                status=RunStatus.RUNNING,
                code_version=code_version,
                created_at=self._now(),
                idempotency_key=self._idempotency_key,
            )
        )
        payload: dict[str, Any] = {
            "workflow_name": self._wf.name,
            "input_ref": encode(self._input),
            "code_version": code_version,
        }
        if self._idempotency_key is not None:
            payload["idempotency_key"] = self._idempotency_key
        await self._commit(
            Event(
                run_id=self._run_id,
                type=EventType.WORKFLOW_CREATED,
                payload=payload,
                ts=self._now(),
            )
        )

    async def _drive(self) -> None:
        engine = ReplayEngine(
            store=self._store,
            run_id=self._run_id,
            injector=self._injector,
            clock=self._clock,
        )
        await engine.drive(self._wf, self._input)

    async def _outcome(self) -> Any:
        events = await self._store.read_events(self._run_id)
        return _outcome_from_events(events, _return_annotation(self._wf.fn))


def _outcome_from_events(events: Sequence[Event], return_annotation: Any) -> Any:
    """Return the rehydrated completed output, or raise the recorded failure."""
    for event in reversed(events):
        if event.type is EventType.WORKFLOW_COMPLETED:
            return rehydrate(event.payload["output_ref"], return_annotation)
        if event.type is EventType.WORKFLOW_FAILED:
            error = event.payload["error"]
            raise WorkflowFailedError(error["type"], error["message"], error["traceback"])
    raise RuntimeError("run did not reach a terminal state")


def build_run_handle(
    workflow: Any,
    workflow_input: Any,
    *,
    run_id: str | None,
    idempotency_key: str | None,
    store: Store,
    injector: FaultInjector | None,
    clock: Clock | None,
) -> RunHandle:
    """Resolve the workflow definition and return a handle wired to a controller."""
    workflow_def = _resolve_workflow(workflow)
    resolved_run_id = run_id or uuid.uuid4().hex
    controller = RunController(
        store=store,
        run_id=resolved_run_id,
        workflow_def=workflow_def,
        workflow_input=workflow_input,
        idempotency_key=idempotency_key,
        injector=injector,
        clock=clock,
    )
    return RunHandle(resolved_run_id, controller)


def _resolve_workflow(workflow: Any) -> WorkflowDefinition:
    from satay.api.decorators import WORKFLOW_ATTR

    definition = getattr(workflow, WORKFLOW_ATTR, None)
    if isinstance(definition, WorkflowDefinition):
        return definition
    raise TypeError(
        f"{getattr(workflow, '__name__', workflow)!r} is not a @satay.workflow; "
        f"decorate it with @satay.workflow before passing it to satay.start"
    )
