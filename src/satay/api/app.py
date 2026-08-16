"""``satay.run_app`` — a journal with the poll loop already running (KAN-491, ADR-0030).

Two of the five primitives park the run: ``satay.sleep`` and ``wait_for_event`` give up
the coroutine entirely, and something has to wake it. Until this module existed, a script
that used either one had to assemble that "something" by hand — open a data dir, open a
:class:`~satay.journal.store.SQLiteStore`, build a
:class:`~satay.timers.TimerEventWorker`, start its loop as a task, and tear all three down
in a ``finally`` — four sub-module imports, none of them public surface, before you could
write the line you actually cared about. This is that block, once, in the runtime::

    async with satay.run_app() as store:
        print(await satay.start(trial, "u-1", store=store).result())

**An async context manager, not a ``run_app(main)`` callback.** Teardown is the whole
problem here (the hand-rolled version needed ``try``/``finally`` and still leaked a
cancelled task on the way out), and ``async with`` is Python's answer to teardown. It also
composes: the body is your code, at your indentation, inside whatever ``asyncio.run`` you
already have — a callback form would have owned the event loop and made the tutorial teach
inversion of control before it taught durability.

**Core, not ``satay[studio]``.** A reader hits ``satay.sleep`` on page two of the tutorial,
long before Studio, so this cannot need the extra (ADR-0013/0016). Nothing here imports
FastAPI, uvicorn or Typer; the store and the worker are imported lazily inside the function
so ``import satay`` stays cheap. ``satay dev`` is the other end of the same idea — a whole
process, with Studio — and the two interoperate over one journal.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import os

    from satay.config import EffectSafety, NondeterminismPolicy, VersionMismatchPolicy
    from satay.journal import Store
    from satay.testing.clock import Clock
    from satay.testing.faults import FaultInjector
    from satay.testing.rng import Rng

#: Poll cadence for the worker ``run_app`` starts, in seconds. Shorter than the one
#: ``satay dev`` uses (1.0s): a script is usually waiting on the loop interactively, and
#: an idle tick over an empty timer table is a couple of indexed SQLite reads.
DEFAULT_POLL_INTERVAL = 0.2


@asynccontextmanager
async def run_app(
    *,
    data_dir: str | os.PathLike[str] | None = None,
    store: Store | None = None,
    interval: float = DEFAULT_POLL_INTERVAL,
    clock: Clock | None = None,
    rng: Rng | None = None,
    injector: FaultInjector | None = None,
    effect_safety: str | EffectSafety | None = None,
    nondeterminism: str | NondeterminismPolicy | None = None,
    version_mismatch: str | VersionMismatchPolicy | None = None,
) -> AsyncIterator[Store]:
    """Open the journal, run the timer + event poll loop, and yield the store (N21).

    Pass the yielded store to every ``satay.start`` / ``satay.send_event`` inside the
    block so the whole app shares one writer (ADR-0012)::

        async with satay.run_app() as store:
            handle = satay.start(trial, "u-1", store=store)
            print(await handle.result())

    While the block is open, ``await handle.result()`` on a run that parks **waits** for
    the loop to wake it and returns the real outcome, instead of handing back
    :data:`satay.PARKED` (ADR-0030) — so a workflow that sleeps or waits for an event
    reads like an ordinary ``await``. A run parked on an event nobody sends waits forever,
    exactly like any other ``await``; wrap it in :func:`asyncio.wait_for` if you want a
    deadline.

    On the way out the loop is stopped and awaited before the store is closed, in that
    order, whether the block exits normally or by exception.

    ``data_dir`` overrides the project-local ``./.satay`` (as ``--data-dir`` /
    ``SATAY_DATA_DIR`` do). ``store`` runs the loop over a store you opened yourself, and
    leaves closing it to you — an in-memory store in a test, say. ``interval`` is the poll
    cadence in seconds. ``clock`` / ``rng`` / ``injector`` and the three policy settings
    are the same seam ``satay.start`` takes; a :class:`~satay.testing.clock.ManualClock`
    here means the loop only advances when your test advances it.
    """
    from satay.config import (
        db_path,
        resolve_data_dir,
        resolve_effect_safety,
        resolve_nondeterminism,
        resolve_version_mismatch,
    )
    from satay.journal.store import SQLiteStore
    from satay.timers import TimerEventWorker, register_poll_loop, unregister_poll_loop

    owned: SQLiteStore | None = None
    if store is None:
        directory = resolve_data_dir(data_dir)
        directory.mkdir(parents=True, exist_ok=True)
        owned = SQLiteStore.open(db_path(directory))
        resolved: Store = owned
    else:
        if data_dir is not None:
            raise TypeError("pass either data_dir= or store= to satay.run_app(), not both")
        resolved = store

    worker = TimerEventWorker(
        store=resolved,
        clock=clock,
        rng=rng,
        injector=injector,
        interval=interval,
        effect_safety=resolve_effect_safety(effect_safety),
        nondeterminism=resolve_nondeterminism(nondeterminism),
        version_mismatch=resolve_version_mismatch(version_mismatch),
    )
    # Registered here rather than only inside ``worker.run()``: the task does not reach
    # its first line until the next suspension point, and a ``result()`` awaited before
    # that would otherwise see no loop and give up on a parked run.
    register_poll_loop(resolved)
    task = asyncio.create_task(worker.run())
    try:
        yield resolved
    finally:
        worker.stop()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        unregister_poll_loop(resolved)
        if owned is not None:
            owned.close()


__all__ = ["DEFAULT_POLL_INTERVAL", "run_app"]
