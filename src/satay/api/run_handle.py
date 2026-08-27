"""The run handle returned by ``satay.start`` (N4).

Drives a run to a terminal state (``result``) and reads current state (``status``).
The heavy lifting — create/resume/no-op decision and the replay drive — lives in a
:class:`RunController` (see :mod:`satay.api.runner`) attached to the handle, so the
public ``satay.api`` package stays free of a heavy import cycle. ``cancel`` lands in
V5.

This module also owns what ``result()`` answers for a run that **parked** on a durable
timer or an event wait and therefore has no outcome yet: :data:`PARKED`, and the
:func:`await_unpark` policy every controller shares (ADR-0030).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Final, Protocol

if TYPE_CHECKING:
    from satay.journal import Store
    from satay.journal.events import RunStatus


class Parked:
    """The type of :data:`PARKED`. Not instantiated anywhere else."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "<parked>"

    def __reduce__(self) -> str:
        """Keep ``PARKED`` a singleton through ``copy`` and ``pickle``.

        The entire contract of this value is that you test it with ``is``, and without
        this both :func:`copy.deepcopy` and a pickle round-trip hand back a *second*
        ``Parked`` instance — one that reprs identically, compares unequal by identity,
        and silently fails every ``is satay.PARKED`` check downstream. Returning the
        global's name tells both protocols to resolve it by lookup instead of rebuilding
        it, which is how ``Ellipsis`` and ``NotImplemented`` stay singletons too.
        """
        return "PARKED"


#: What ``await handle.result()`` returns for a run that is parked on a durable timer or
#: an event wait with no poll loop in this process to wake it (ADR-0030).
#:
#: It used to return ``None``, which is indistinguishable from a workflow that returned
#: ``None`` on purpose — the caller could not tell "no outcome yet" from "the outcome is
#: ``None``" without a second ``status()`` call. Test for it by identity::
#:
#:     if await handle.result() is satay.PARKED:
#:         ...  # nothing has happened yet; the run is waiting on a timer or an event
#:
#: Inside ``async with satay.run_app()`` you will not see it: ``result()`` waits for the
#: running poll loop to unpark the run and returns the real outcome instead.
PARKED: Final[Parked] = Parked()

#: How often :func:`await_unpark` re-reads the run row while a poll loop works on it.
#: Real seconds, not clock seconds: it is waiting on a background task, not on durable
#: time, so a :class:`~satay.testing.clock.ManualClock` must not be able to freeze it.
_UNPARK_POLL_SECONDS = 0.02


async def await_unpark(store: Store, run_id: str) -> bool:
    """Return whether the run is still parked, waiting for a poll loop first (ADR-0030).

    Called by a controller once its drive has returned, to decide between the recorded
    outcome and :data:`PARKED`:

    - the run is not ``waiting`` — it finished, or it never parked: ``False``, read the
      outcome;
    - it is ``waiting`` and a poll loop is running over this store in this process: wait
      for that loop to fire the timer or deliver the event, then ``False``;
    - it is ``waiting`` and nothing here will wake it: ``True`` — hand back ``PARKED``
      rather than a fake result.

    A run parked on an event nobody ever sends waits forever, which is what awaiting it
    means. :func:`asyncio.wait_for` bounds it if a caller wants a deadline.

    The one drive that must never wait is the poll loop's own: a control-plane ``start``
    is applied inside a tick, and a tick that waited for itself to make progress would
    hang the worker. Inside a tick this always answers "still parked" immediately.
    """
    from satay.journal.events import TERMINAL_STATUSES, RunStatus
    from satay.timers import in_poll_loop_tick, poll_loop_running

    record = await store.get_run(run_id)
    if record is None or record.status is not RunStatus.WAITING:
        return False
    if in_poll_loop_tick():
        return True
    while poll_loop_running(store):
        await asyncio.sleep(_UNPARK_POLL_SECONDS)
        record = await store.get_run(run_id)
        if record is None or record.status in TERMINAL_STATUSES:
            return False
    return True


class WorkflowFailedError(RuntimeError):
    """Raised by ``result()`` when the run terminated in ``WorkflowFailed``.

    Carries the recorded error type, message, and native traceback string so callers
    and the CLI surface the original failure.
    """

    def __init__(self, error_type: str, message: str, tb: str) -> None:
        super().__init__(f"{error_type}: {message}")
        self.error_type = error_type
        self.error_message = message
        self.traceback_str = tb


class RunController(Protocol):
    """The drive/read backend a :class:`RunHandle` delegates to."""

    async def result(self) -> Any: ...

    async def status(self) -> RunStatus: ...

    async def cancel(self) -> None: ...

    def current_run_id(self) -> str: ...


class RunHandle:
    """Handle to a durable run (N4)."""

    def __init__(self, run_id: str, controller: RunController | None = None) -> None:
        self._run_id = run_id
        self._controller = controller

    @property
    def run_id(self) -> str:
        """The run id. A keyed idempotent start may resolve it once driven (N13)."""
        if self._controller is not None:
            return self._controller.current_run_id()
        return self._run_id

    async def result(self) -> Any:
        """Drive the run to a terminal state and return/raise its outcome.

        Raises :class:`WorkflowFailedError` for a failed run. For a run that **parks** on
        a durable timer or an event wait, this waits for a poll loop running in this
        process (``satay.run_app``, or a ``TimerEventWorker.run()`` of your own) to wake
        it and returns the real outcome; with no such loop it returns :data:`PARKED`,
        which is *not* ``None`` and cannot be confused with one (ADR-0030).
        """
        if self._controller is None:  # pragma: no cover - defensive
            raise RuntimeError("run handle is not attached to a controller")
        return await self._controller.result()

    async def status(self) -> RunStatus:
        """Read the run's current status without driving it.

        Returns the :class:`~satay.journal.events.RunStatus` member, not a bare string,
        so a comparison can be typo-proof (``is RunStatus.COMPLETED``) and ``mypy`` can
        check an exhaustive ``match``. ``RunStatus`` is a :class:`enum.StrEnum`, so the
        older ``== "completed"`` form keeps working and keeps printing as before
        (KAN-524).
        """
        if self._controller is None:  # pragma: no cover - defensive
            raise RuntimeError("run handle is not attached to a controller")
        return await self._controller.status()

    async def cancel(self) -> None:
        """Cancel the run: append ``WorkflowCancelled`` and halt it (N4, V5).

        Reaches the *same* journal transition as the HTTP cancel endpoint. A no-op for
        an unknown or already-terminal run.
        """
        if self._controller is None:  # pragma: no cover - defensive
            raise RuntimeError("run handle is not attached to a controller")
        await self._controller.cancel()
