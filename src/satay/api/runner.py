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
from satay.config import EffectSafety, NondeterminismPolicy
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
from satay.testing.rng import Rng
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
        key_lookup: bool,
        injector: FaultInjector | None,
        clock: Clock | None,
        rng: Rng | None,
        effect_safety: EffectSafety,
        nondeterminism: NondeterminismPolicy,
    ) -> None:
        self._store = store
        self._run_id = run_id
        self._wf = workflow_def
        self._input = workflow_input
        self._idempotency_key = idempotency_key
        self._key_lookup = key_lookup
        self._injector = injector
        self._clock = clock
        self._rng = rng
        self._effect_safety = effect_safety
        self._nondeterminism = nondeterminism

    def current_run_id(self) -> str:
        """The resolved run id (may change once a keyed start resolves, N13)."""
        return self._run_id

    def _now(self) -> Any:
        return self._clock.now() if self._clock is not None else utc_now()

    async def _commit(self, event: Event) -> Event:
        stored = await self._store.append(event)
        if self._injector is not None:
            await self._injector.reached(stored.type.value)
        return stored

    async def _resolve_keyed_run(self) -> None:
        """Resolve a keyed idempotent start to any existing run (N13, build step 5)."""
        if not self._key_lookup or self._idempotency_key is None:
            return
        existing = await self._store.get_run_by_idempotency_key(self._idempotency_key)
        if existing is not None:
            # Repeated key → the same logical run: resume it or return its result.
            self._run_id = existing.run_id

    async def result(self) -> Any:
        """Ensure the run exists, drive it if non-terminal, return/raise the outcome."""
        await self._resolve_keyed_run()
        record = await self._store.get_run(self._run_id)

        if record is None:
            await self._create()
            await self._drive()
        elif record.status in TERMINAL_STATUSES:
            # Terminal no-op: return the recorded outcome without re-driving.
            pass
        elif record.status is RunStatus.WAITING:
            # A durably parked run (sleep / wait_for_event): re-driving is a graceful
            # wake, so it writes NO WorkflowResumed and carries no ⚡ (ADR-0009/Q52).
            await self._drive()
        else:
            # Resume a run interrupted mid-execution (not durably parked). Before
            # re-driving, apply the version-mismatch policy (N17): strict rejects the
            # resume, dev warns and continues (offering a fork). The WorkflowResumed
            # event is what renders the ⚡ marker (ADR-0009/Q52), appended only once the
            # resume is allowed to proceed.
            from satay.versioning import check_resume_version, current_code_version

            check_resume_version(record.code_version, current_code_version(), self._effect_safety)
            await self._commit(
                Event(run_id=self._run_id, type=EventType.WORKFLOW_RESUMED, ts=self._now())
            )
            await self._drive()

        # A run still parked (WAITING) has no terminal outcome yet: return None. The
        # worker's poll loop drives it to completion when its timer/event resolves; a
        # later result() then returns the recorded outcome (terminal no-op above).
        record = await self._store.get_run(self._run_id)
        if record is not None and record.status is RunStatus.WAITING:
            return None
        return await self._outcome()

    async def status(self) -> str:
        """Return the run's current status ('running' until the row exists)."""
        await self._resolve_keyed_run()
        record = await self._store.get_run(self._run_id)
        if record is None:
            return RunStatus.RUNNING.value
        return record.status.value

    async def cancel(self) -> None:
        """Append ``WorkflowCancelled`` and halt the run (N4, V5).

        Delegates to the shared :func:`~satay.control.commands.append_cancellation`, so
        the in-process handle path and the HTTP ``cancel`` command reach the identical
        journal transition. Imported lazily to avoid a package import cycle.
        """
        from satay.control.commands import append_cancellation

        await self._resolve_keyed_run()
        await append_cancellation(
            self._store, self._run_id, now=self._now(), injector=self._injector
        )

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
            rng=self._rng,
            effect_safety=self._effect_safety,
            nondeterminism=self._nondeterminism,
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
    rng: Rng | None,
    effect_safety: EffectSafety,
    nondeterminism: NondeterminismPolicy,
) -> RunHandle:
    """Resolve the workflow definition and return a handle wired to a controller."""
    workflow_def = _resolve_workflow(workflow)
    resolved_run_id = run_id or uuid.uuid4().hex
    # A keyed start (idempotency_key, no explicit run_id) resolves to any existing run
    # with that key; an explicit run_id is an unambiguous resume-by-id and wins.
    key_lookup = idempotency_key is not None and run_id is None
    controller = RunController(
        store=store,
        run_id=resolved_run_id,
        workflow_def=workflow_def,
        workflow_input=workflow_input,
        idempotency_key=idempotency_key,
        key_lookup=key_lookup,
        injector=injector,
        clock=clock,
        rng=rng,
        effect_safety=effect_safety,
        nondeterminism=nondeterminism,
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
