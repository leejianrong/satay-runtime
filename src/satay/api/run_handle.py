"""The run handle returned by ``satay.start``.

Drives a run to a terminal state (``result``) and reads current state (``status``).
``cancel`` lands in V5. Behaviour lands in V1; this is the public shape.
"""

from __future__ import annotations

from typing import Any


class RunHandle:
    """Handle to a durable run (N4). Public surface; behaviour lands in V1."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id

    async def result(self) -> Any:
        """Drive the run to a terminal state and return/raise its outcome (lands in V1)."""
        raise NotImplementedError("RunHandle.result lands in V1")

    async def status(self) -> str:
        """Read the run's current status without driving it (lands in V1)."""
        raise NotImplementedError("RunHandle.status lands in V1")

    async def cancel(self) -> None:
        """Request cancellation of the run (lands in V5)."""
        raise NotImplementedError("RunHandle.cancel lands in V5")
