"""The run handle returned by ``satay.start`` (N4).

Drives a run to a terminal state (``result``) and reads current state (``status``).
The heavy lifting — create/resume/no-op decision and the replay drive — lives in a
:class:`RunController` (see :mod:`satay.api.runner`) attached to the handle, so the
public ``satay.api`` package stays free of a heavy import cycle. ``cancel`` lands in
V5.
"""

from __future__ import annotations

from typing import Any, Protocol


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

    async def status(self) -> str: ...

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
        """Drive the run to a terminal state and return/raise its outcome."""
        if self._controller is None:  # pragma: no cover - defensive
            raise RuntimeError("run handle is not attached to a controller")
        return await self._controller.result()

    async def status(self) -> str:
        """Read the run's current status without driving it."""
        if self._controller is None:  # pragma: no cover - defensive
            raise RuntimeError("run handle is not attached to a controller")
        return await self._controller.status()

    async def cancel(self) -> None:
        """Request cancellation of the run (lands in V5)."""
        raise NotImplementedError("RunHandle.cancel lands in V5")
