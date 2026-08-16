"""The ``satay dev`` dev-stack orchestrator (A9, N20) — **satay[studio] only**.

Assembles the parts V1-V7 already prove into one process: the asyncio worker poll loop,
the SQLite store (with blob spill), the HTTP control + read API, and the served Studio
SPA. It is pure assembly — no new runtime behaviour — so a run behaves identically to
one started by hand (the V8 regression point).

Startup order is store → worker → HTTP server; shutdown is the exact reverse, then the
data-directory lock is released. Before anything opens the store it takes an exclusive
advisory lock on the data directory (:mod:`satay.devstack.lock`), so a second
``satay dev`` on the same ``./.satay/`` is refused rather than racing the single-writer
journal into corruption (ADR-0017/Q54).

The booted stack mints a per-session token (ADR-0014) and prints a **tokenized Studio
URL** — the Studio SPA reads ``?token=`` from its own location — which is the V6 token
hand-off deferred to here and the Q43 session-token smoke path.

Before any of that, :func:`run_dev` imports the user's workflow modules
(:mod:`satay.devstack.appload`, driven by ``--app``), because the registry is only ever
populated by decorators running at import time: without that step the worker has no
workflow to resolve when a timer comes due and the control ``start`` endpoint has
nothing to start (KAN-448). The boot prints what got registered, and a module that
cannot be imported aborts the boot instead of yielding a silently empty registry.

This module imports FastAPI/uvicorn at load, so it is **never** imported at core import
time: ``satay.devstack.__init__`` reaches it through a lazy accessor and the core CLI
only loads it when actually running ``satay dev``.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sys
from collections.abc import Sequence
from pathlib import Path

import uvicorn

from satay.config import (
    EffectSafety,
    NondeterminismPolicy,
    VersionMismatchPolicy,
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
from satay.testing.clock import Clock, RealClock
from satay.timers import TimerEventWorker

#: Default loopback port for ``satay dev`` (overridable with ``--port``; ``0`` = ephemeral).
DEFAULT_PORT = 8787


class _NoSignalServer(uvicorn.Server):
    """A uvicorn server that installs no signal handlers — the dev stack owns shutdown.

    uvicorn's own handlers only work on the main thread and would fight the orchestrator's
    explicit start/stop, so we disable them and drive ``should_exit`` ourselves.
    """

    def install_signal_handlers(self) -> None:
        return None


class DevStack:
    """One-process dev stack: worker + SQLite + control/read API + Studio (N20).

    Start with :meth:`start` (or ``async with``); stop with :meth:`stop`. The parts start
    in a clean order and stop in the exact reverse, and the data-dir lock brackets the
    whole lifetime.
    """

    def __init__(
        self,
        *,
        data_dir: Path | str,
        host: str = "127.0.0.1",
        port: int = DEFAULT_PORT,
        token: str | None = None,
        clock: Clock | None = None,
        worker_interval: float = 1.0,
        log_level: str = "info",
        effect_safety: str | EffectSafety | None = None,
        nondeterminism: str | NondeterminismPolicy | None = None,
        version_mismatch: str | VersionMismatchPolicy | None = None,
    ) -> None:
        ensure_loopback_bind(host)
        self._data_dir = Path(data_dir)
        self._host = host
        self._port = port
        self._token = token or generate_token()
        self._clock = clock or RealClock()
        self._worker_interval = worker_interval
        self._log_level = log_level
        # Resolve all three project policies the way the in-process API does — explicit
        # override, then SATAY_EFFECT_SAFETY / SATAY_NONDETERMINISM / SATAY_VERSION_MISMATCH,
        # then the documented default — and hand them to the worker, which otherwise
        # silently took its own defaults and ignored the environment (KAN-458). Any policy
        # added to the worker must be threaded here too, or `satay dev` quietly stops
        # honouring it.
        self._effect_safety = resolve_effect_safety(effect_safety)
        self._nondeterminism = resolve_nondeterminism(nondeterminism)
        self._version_mismatch = resolve_version_mismatch(version_mismatch)

        self._lock = DataDirLock(self._data_dir)
        self._store: SQLiteStore | None = None
        self._queue: CommandQueue | None = None
        self._worker: TimerEventWorker | None = None
        self._worker_task: asyncio.Task[None] | None = None
        self._server: uvicorn.Server | None = None
        self._server_task: asyncio.Task[None] | None = None
        self._bound_port: int | None = None
        #: The parts that have started, in order — asserted by the clean-order test.
        self.started_parts: list[str] = []

    # -- properties --------------------------------------------------------------

    @property
    def token(self) -> str:
        """The per-session token the guarded API requires (ADR-0014)."""
        return self._token

    @property
    def effect_safety(self) -> EffectSafety:
        """The resolved effect-safety mode the worker runs under (ADR-0006)."""
        return self._effect_safety

    @property
    def nondeterminism(self) -> NondeterminismPolicy:
        """The resolved replay-divergence policy the worker runs under (ADR-0022)."""
        return self._nondeterminism

    @property
    def version_mismatch(self) -> VersionMismatchPolicy:
        """The resolved code-version mismatch policy the worker runs under (ADR-0023)."""
        return self._version_mismatch

    @property
    def port(self) -> int:
        """The actually-bound port (resolved after :meth:`start`, even for port 0)."""
        return self._bound_port if self._bound_port is not None else self._port

    def base_url(self) -> str:
        """The control/read API base URL."""
        return f"http://{self._host}:{self.port}"

    def studio_url(self) -> str:
        """The tokenized Studio URL the SPA reads ``?token=`` from (ADR-0014 hand-off)."""
        return f"{self.base_url()}/?token={self._token}"

    # -- lifecycle ---------------------------------------------------------------

    async def start(self) -> None:
        """Acquire the lock and start store → worker → HTTP server, in that order."""
        self._lock.acquire()  # raises DataDirLockedError if a second dev holds it
        self.started_parts.append("lock")

        self._store = SQLiteStore.open(db_path(self._data_dir))
        self.started_parts.append("store")

        self._queue = CommandQueue()
        self._worker = TimerEventWorker(
            store=self._store,
            clock=self._clock,
            commands=self._queue,
            interval=self._worker_interval,
            effect_safety=self._effect_safety,
            nondeterminism=self._nondeterminism,
            version_mismatch=self._version_mismatch,
        )
        self._worker_task = asyncio.create_task(self._worker.run())
        self.started_parts.append("worker")

        app = create_app(
            store=self._store,
            command_queue=self._queue,
            security=SecurityPolicy(token=self._token),
            redactor=Redactor(),
        )
        config = uvicorn.Config(app, host=self._host, port=self._port, log_level=self._log_level)
        self._server = _NoSignalServer(config)
        self._server_task = asyncio.create_task(self._server.serve())
        await self._await_server_started()
        self.started_parts.append("server")

    async def _await_server_started(self) -> None:
        assert self._server is not None
        while not self._server.started:
            if self._server_task is not None and self._server_task.done():
                # Surface a bind failure instead of spinning forever.
                self._server_task.result()
            await asyncio.sleep(0.01)
        self._bound_port = self._server.servers[0].sockets[0].getsockname()[1]

    async def stop(self) -> None:
        """Stop server → worker → store (reverse of start), then release the lock."""
        if self._server is not None:
            self._server.should_exit = True
        if self._server_task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._server_task
            self._server_task = None
        if self._worker is not None:
            self._worker.stop()
        if self._worker_task is not None:
            self._worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker_task
            self._worker_task = None
        if self._store is not None:
            self._store.close()
            self._store = None
        self._lock.release()

    async def __aenter__(self) -> DevStack:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()


def _say(line: str) -> None:
    """Print a boot line and **flush** it (KAN-491).

    Every line ``satay dev`` prints is printed once and then the process blocks until
    Ctrl-C. Python only line-buffers stdout when it is a tty, so redirected —
    ``satay dev > dev.log``, a CI step, an agent capturing the output — the whole banner
    sat in an 8 KiB buffer that nothing would flush for as long as the stack ran, and the
    tokenized Studio URL, which is the one thing you cannot reconstruct by hand, never
    arrived. Flushing per line costs nothing here and makes the output honest under a
    pipe.
    """
    print(line, flush=True)


async def _serve_until_signalled(stack: DevStack) -> None:
    """Run the stack until SIGINT/SIGTERM, printing the URLs on start."""
    await stack.start()
    _say(f"Satay Studio:  {stack.studio_url()}")
    _say(f"  control/read API on {stack.base_url()}  (session token required)")
    _say("  press Ctrl-C to stop")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # not all platforms/loops support it
            loop.add_signal_handler(sig, stop.set)
    try:
        await stop.wait()
    finally:
        await stack.stop()


def run_dev(
    *,
    data_dir: str | None = None,
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    log_level: str = "info",
    app_modules: Sequence[str] | None = None,
    project_dir: str | None = None,
) -> int:
    """Boot the dev stack and block until interrupted; return a process exit code (U1).

    Imports the user's ``app_modules`` **first** (KAN-448) so the registry is populated
    before the worker's poll loop and the control API come up. A module that cannot be
    imported aborts with exit code 2 and a message naming it; a successful boot prints
    what it registered, including the honest ``0 workflows`` case.
    """
    from satay.devstack.appload import AppImportError, load_app
    from satay.devstack.lock import DataDirLockedError

    try:
        report = load_app(app_modules, project_dir=project_dir)
    except AppImportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    resolved = resolve_data_dir(data_dir)
    stack = DevStack(data_dir=resolved, host=host, port=port, log_level=log_level)
    for line in report.describe():
        _say(line)
    _say(
        f"policies: effect_safety={stack.effect_safety.value}, "
        f"nondeterminism={stack.nondeterminism.value}, "
        f"version_mismatch={stack.version_mismatch.value}"
    )
    try:
        asyncio.run(_serve_until_signalled(stack))
    except DataDirLockedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    return 0


__all__ = ["DEFAULT_PORT", "DevStack", "run_dev"]
