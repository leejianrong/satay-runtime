"""``satay.fork`` — fork a finished run from code (KAN-481, ADR-0028).

Forking used to be reachable only through the control plane: build a ``ControlAPI``
and a ``CommandQueue``, enqueue a fork at a raw journal ``seq`` you had computed
yourself, ``tick()`` a worker to apply it, then issue a no-op ``satay.start`` to read
the result. Four objects for one idea. This module is that idea in one call::

    handle = await satay.fork(run_id, before_task="synthesize", workflow_input=brief)
    print(await handle.result())

**Which side of the core-dependency boundary?** The core (ADR-0013/0016). ``fork`` is
part of the debugger wedge ADR-0025 puts first, so it must not need ``satay[studio]``.
The seeding and fork-point logic already lives in :mod:`satay.control.commands`, which
is pure Python for exactly this reason, and is imported **lazily** here — the same
arrangement ``RunHandle.cancel()`` already uses. ``import satay`` still pulls no
FastAPI, uvicorn, Pydantic, Typer or Click.

**Who writes?** The caller, in-process, exactly like ``satay.start``. ADR-0012's
single-writer rule is about the HTTP thread not writing behind the worker's back; it
does not turn the in-process API into a second writer. A fork driven through the HTTP
route still goes through the command queue and the worker, and both paths converge on
:func:`~satay.control.commands.drive_forked_run`.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from satay.api.run_handle import PARKED, RunHandle, await_unpark

if TYPE_CHECKING:
    from satay.config import EffectSafety, NondeterminismPolicy, VersionMismatchPolicy
    from satay.journal import Store
    from satay.journal.events import RunStatus
    from satay.testing.clock import Clock
    from satay.testing.faults import FaultInjector
    from satay.testing.rng import Rng


class _Inherited:
    """The type of :data:`_INHERITED`."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - signature/debugging aid
        return "<inherited>"


#: Default for ``workflow_input=``: inherit the source run's recorded input. A sentinel
#: rather than ``None`` because ``None`` is a valid workflow input, so
#: ``fork(..., workflow_input=None)`` must mean "run it with ``None``".
_INHERITED: Any = _Inherited()


async def fork(
    source_run_id: str,
    *,
    before_task: str | None = None,
    before_ordinal: int | None = None,
    fork_point_seq: int | None = None,
    workflow_input: Any = _INHERITED,
    run_id: str | None = None,
    store: Store | None = None,
    injector: FaultInjector | None = None,
    clock: Clock | None = None,
    rng: Rng | None = None,
    effect_safety: str | EffectSafety | None = None,
    nondeterminism: str | NondeterminismPolicy | None = None,
    version_mismatch: str | VersionMismatchPolicy | None = None,
) -> RunHandle:
    """Fork a terminal run into a new one and return its handle (N15, KAN-481).

    The new run's journal is seeded here — so the fork exists, and any validation error
    is raised, before this returns — and driven on ``await handle.result()``, re-running
    every durable call after the fork point while reusing the copied prefix as journal
    hits. The source run is never modified (ADR-0004).

    **Choosing the fork point.** Exactly one of:

    - ``before_task="synthesize"`` — cut so that task re-runs. Scheduled more than once?
      The **earliest** occurrence is chosen; name an occurrence with ``before_ordinal=``.
    - ``fork_point_seq=`` — an explicit journal ``seq``, kept **inclusive** (the last
      event copied), which is what Studio sends when you click an event.

    **Changing the input.** ``workflow_input=`` runs the fork under a different input —
    the "same run, sharper prompt" story. It is recorded in the fork's own
    ``WorkflowCreated``, so it survives a park-and-wake or a crash, and the fork's
    lineage records ``input_overridden``. The override cannot rewrite history: the
    copied prefix stays exactly what the source recorded, so only calls **after** the
    fork point see the new input. Put the fork point before the first durable call that
    should see it. If the new input would have made the prefix call *different tasks*,
    strict nondeterminism (the default, ADR-0022) raises rather than splicing two
    incompatible histories together — fork earlier. See ADR-0028.

    ``store`` / ``injector`` / ``clock`` / ``rng`` and the three policy settings behave
    as they do on ``satay.start``.
    """
    from satay.api.primitives import _default_store
    from satay.api.registry import REGISTRY
    from satay.config import (
        resolve_effect_safety,
        resolve_nondeterminism,
        resolve_version_mismatch,
    )
    from satay.control.commands import (
        INHERIT,
        UnknownWorkflowError,
        create_fork,
        resolve_fork_point,
    )
    from satay.journal.events import utc_now

    resolved_store = store if store is not None else _default_store()
    seq = await resolve_fork_point(
        resolved_store,
        source_run_id,
        fork_point_seq=fork_point_seq,
        before_task=before_task,
        before_ordinal=before_ordinal,
    )

    record = await resolved_store.get_run(source_run_id)
    assert record is not None  # resolve_fork_point already rejected an unknown run
    if REGISTRY.get_workflow(record.workflow_name) is None:
        # Checked before seeding so a typo'd import never leaves an undriveable run row.
        raise UnknownWorkflowError(record.workflow_name)

    new_run_id = run_id or uuid.uuid4().hex
    workflow_name = await create_fork(
        resolved_store,
        source_run_id=source_run_id,
        fork_point_seq=seq,
        new_run_id=new_run_id,
        now=clock.now() if clock is not None else utc_now(),
        workflow_input=INHERIT if isinstance(workflow_input, _Inherited) else workflow_input,
    )
    controller = ForkController(
        store=resolved_store,
        run_id=new_run_id,
        workflow_name=workflow_name,
        injector=injector,
        clock=clock,
        rng=rng,
        effect_safety=resolve_effect_safety(effect_safety),
        nondeterminism=resolve_nondeterminism(nondeterminism),
        version_mismatch=resolve_version_mismatch(version_mismatch),
    )
    return RunHandle(new_run_id, controller)


class ForkController:
    """Backs the :class:`~satay.api.run_handle.RunHandle` returned by :func:`fork`.

    A sibling of :class:`~satay.api.runner.RunController` with one deliberate
    difference: driving a freshly seeded fork appends **no** ``WorkflowResumed``, so the
    fork carries no ⚡ interruption marker (ADR-0009/Q52). It is a new run, not a crash
    recovery — the same reason ``apply_fork`` drives the engine directly.
    """

    def __init__(
        self,
        *,
        store: Store,
        run_id: str,
        workflow_name: str,
        injector: FaultInjector | None,
        clock: Clock | None,
        rng: Rng | None,
        effect_safety: EffectSafety,
        nondeterminism: NondeterminismPolicy,
        version_mismatch: VersionMismatchPolicy,
    ) -> None:
        self._store = store
        self._run_id = run_id
        self._workflow_name = workflow_name
        self._injector = injector
        self._clock = clock
        self._rng = rng
        self._effect_safety = effect_safety
        self._nondeterminism = nondeterminism
        self._version_mismatch = version_mismatch

    def current_run_id(self) -> str:
        """The forked run's id (fixed: a fork never resolves to another run)."""
        return self._run_id

    async def result(self) -> Any:
        """Drive the fork to a terminal state and return/raise its outcome.

        A fork that parks behaves exactly like any other run: it waits for a poll loop
        running in this process, or returns :data:`satay.PARKED` (ADR-0030).
        """
        from satay.api.registry import REGISTRY
        from satay.api.runner import _outcome_from_events
        from satay.control.commands import drive_forked_run
        from satay.journal.events import TERMINAL_STATUSES
        from satay.replay.engine import _return_annotation

        record = await self._store.get_run(self._run_id)
        if record is not None and record.status not in TERMINAL_STATUSES:
            await drive_forked_run(
                self._store,
                self._run_id,
                workflow_name=self._workflow_name,
                clock=self._clock,
                rng=self._rng,
                injector=self._injector,
                effect_safety=self._effect_safety,
                nondeterminism=self._nondeterminism,
                version_mismatch=self._version_mismatch,
            )

        # A fork that parked on a timer or an event has no outcome yet: wait for a poll
        # loop if one is running, else answer PARKED — the identical policy
        # ``satay.start`` applies, from the one helper, so the two handles never disagree
        # about what a parked run returns (ADR-0030).
        if await await_unpark(self._store, self._run_id):
            return PARKED
        workflow_def = REGISTRY.get_workflow(self._workflow_name)
        annotation = _return_annotation(workflow_def.fn) if workflow_def is not None else None
        return _outcome_from_events(await self._store.read_events(self._run_id), annotation)

    async def status(self) -> RunStatus:
        """Read the fork's current status without driving it."""
        from satay.journal.events import RunStatus

        record = await self._store.get_run(self._run_id)
        return RunStatus.RUNNING if record is None else record.status

    async def cancel(self) -> None:
        """Cancel the fork — the same journal transition as every other cancel path."""
        from satay.control.commands import append_cancellation
        from satay.journal.events import utc_now

        now = self._clock.now() if self._clock is not None else utc_now()
        await append_cancellation(self._store, self._run_id, now=now, injector=self._injector)


__all__ = ["ForkController", "fork"]
