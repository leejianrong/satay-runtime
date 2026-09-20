"""``satay.control.run_app`` — ``run_app``, plus the control/read API (ADR-0046).

Reuses the exact composition :class:`~satay.devstack.orchestrator.DevStack` already
proves (store -> a :class:`~satay.control.commands.CommandQueue`-draining
:class:`~satay.timers.TimerEventWorker` -> the control/read HTTP server, ADR-0012),
scoped to one ``async with`` block instead of a whole owned process: no ``--app``
module loading, no ``Ctrl-C`` wait loop, ends when the caller's own work ends. See
ADR-0046 for why this is a third shape rather than a parameter on
:func:`satay.run_app` or a reason to reach for :class:`DevStack` directly.

Imports FastAPI/uvicorn at *this* module's load, same as :mod:`satay.control.server` —
never at ``satay.control`` package import time (ADR-0013/0016). ``satay.control``
reaches this through the same lazy-forwarding pattern it already uses for
:func:`~satay.control.server.create_app`/:func:`~satay.control.server.serve`.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

import uvicorn

if TYPE_CHECKING:
    import os

    from satay.config import EffectSafety, NondeterminismPolicy, VersionMismatchPolicy
    from satay.journal import Store
    from satay.testing.clock import Clock
    from satay.testing.faults import FaultInjector
    from satay.testing.rng import Rng

#: Same default as satay.run_app (ADR-0030) — a script is usually waiting on the loop
#: interactively, faster than satay dev's 1.0s cadence.
DEFAULT_POLL_INTERVAL = 0.2


@dataclass(frozen=True, slots=True)
class ControlledApp:
    """What :func:`run_app` yields: the shared store, plus how to reach its control API.

    Distinct from a bare :class:`~satay.journal.Store` (what core ``satay.run_app``
    yields) on purpose — a caller of *this* function needs both, and returning two
    different shapes from two functions of almost the same name is safer than one
    function whose return type depends on a flag.
    """

    store: Store
    base_url: str
    token: str


class _NoSignalServer(uvicorn.Server):
    """A uvicorn server that installs no signal handlers.

    Matches :class:`~satay.devstack.orchestrator.DevStack`'s own ``_NoSignalServer``:
    uvicorn's handlers only work on the main thread and would fight this block's own
    ``async with``/``finally`` shutdown, so they're disabled and ``should_exit`` is
    driven explicitly instead.
    """

    def install_signal_handlers(self) -> None:
        return None


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
    host: str = "127.0.0.1",
    port: int = 0,
    token: str | None = None,
    allowed_origins: frozenset[str] = frozenset(),
    log_level: str = "warning",
) -> AsyncIterator[ControlledApp]:
    """``satay.run_app``, plus the control/read HTTP API for the life of the block (ADR-0046).

    Every ``satay.run_app`` keyword works identically here (``data_dir``, ``store``,
    ``interval``, ``clock``, ``rng``, ``injector``, and the three policy overrides) —
    see its own docstring for what each does. The rest configure the embedded server,
    with the same defaults :func:`~satay.control.server.serve`/``DevStack`` already use:
    loopback-only (refuses a non-loopback ``host``, ADR-0014), an ephemeral port, and a
    freshly generated session token when none is given.

    Yields a :class:`ControlledApp`, not a bare store — pass ``app.store`` to
    ``satay.start``/``satay.send_event`` exactly as you would the value
    ``satay.run_app`` yields, and hand ``app.base_url``/``app.token`` to whatever
    needs to reach this run from outside the process (a second CLI invocation, a
    browser, Satay Studio itself).

    ``data_dir``/``store`` are mutually exclusive, identically to ``satay.run_app``.
    When this call opens its own store (``store=None``), it also takes the ADR-0017/Q54
    data-directory lock ``satay dev`` uses — two writers on one ``./.satay/`` is the
    exact corruption that lock exists to prevent, and being block-scoped doesn't change
    that risk. A caller-supplied ``store`` skips the lock, the same way ``satay.run_app``
    leaves a caller-supplied store's lifetime (and any locking around it) to the caller.
    """
    from satay.config import (
        db_path,
        resolve_data_dir,
        resolve_effect_safety,
        resolve_nondeterminism,
        resolve_version_mismatch,
    )
    from satay.control.commands import CommandQueue
    from satay.control.redaction import Redactor
    from satay.control.security import SecurityPolicy, ensure_loopback_bind, generate_token
    from satay.control.server import create_app
    from satay.devstack.lock import DataDirLock
    from satay.journal.store import SQLiteStore
    from satay.timers import TimerEventWorker, register_poll_loop, unregister_poll_loop

    ensure_loopback_bind(host)

    owned: SQLiteStore | None = None
    lock: DataDirLock | None = None
    if store is None:
        directory = resolve_data_dir(data_dir)
        directory.mkdir(parents=True, exist_ok=True)
        lock = DataDirLock(directory)
        lock.acquire()  # raises DataDirLockedError on contention (ADR-0017/Q54)
        owned = SQLiteStore.open(db_path(directory))
        resolved: Store = owned
    else:
        if data_dir is not None:
            raise TypeError("pass either data_dir= or store= to satay.control.run_app(), not both")
        resolved = store

    queue = CommandQueue()
    worker = TimerEventWorker(
        store=resolved,
        clock=clock,
        rng=rng,
        injector=injector,
        interval=interval,
        commands=queue,
        effect_safety=resolve_effect_safety(effect_safety),
        nondeterminism=resolve_nondeterminism(nondeterminism),
        version_mismatch=resolve_version_mismatch(version_mismatch),
    )
    # Registered before the worker's own first tick, same reasoning as satay.run_app
    # (ADR-0030 §3): a result() awaited immediately must not lose the race.
    register_poll_loop(resolved)
    worker_task = asyncio.create_task(worker.run())

    resolved_token = token or generate_token()
    security = SecurityPolicy(token=resolved_token, allowed_origins=allowed_origins)
    app = create_app(store=resolved, command_queue=queue, security=security, redactor=Redactor())
    config = uvicorn.Config(app, host=host, port=port, log_level=log_level)
    server = _NoSignalServer(config)
    server_task = asyncio.create_task(server.serve())
    await _await_server_started(server, server_task)
    bound_port = server.servers[0].sockets[0].getsockname()[1]

    try:
        yield ControlledApp(
            store=resolved, base_url=f"http://{host}:{bound_port}", token=resolved_token
        )
    finally:
        # Reverse of startup: server, then worker, then the store this call opened —
        # identical ordering to both satay.run_app and DevStack.stop().
        server.should_exit = True
        with contextlib.suppress(asyncio.CancelledError):
            await server_task
        worker.stop()
        worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker_task
        unregister_poll_loop(resolved)
        if owned is not None:
            owned.close()
        if lock is not None:
            lock.release()


async def _await_server_started(server: uvicorn.Server, task: asyncio.Task[None]) -> None:
    """Block until uvicorn has actually bound its socket, surfacing a bind failure."""
    while not server.started:
        if task.done():
            task.result()  # a bind failure (e.g. port in use) raises here, not hangs
        await asyncio.sleep(0.01)


__all__ = ["ControlledApp", "run_app"]
