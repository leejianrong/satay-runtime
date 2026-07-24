"""The current-drive context (N6 wiring).

A ``@satay.task`` call inside a running workflow must not execute inline — it routes
to the replay engine driving the current run. The engine installs itself into a
``ContextVar`` for the duration of a drive; the task wrapper reads it and delegates
its durable call. Using a ``ContextVar`` keeps drives isolated per-task/async-context
without threading a handle through user signatures.
"""

from __future__ import annotations

from contextvars import ContextVar
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from satay.api.registry import TaskDefinition


class Driver(Protocol):
    """What a ``@satay.task`` call or a durable primitive delegates to during a drive."""

    async def durable_call(
        self,
        definition: TaskDefinition,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        """Resolve identity, consult the journal (hit → reuse, miss → execute)."""
        ...

    async def durable_sleep(self, duration: timedelta) -> None:
        """Durably sleep: hit (``TimerFired``) returns; miss parks the run (V3, N5)."""
        ...

    async def durable_wait_for_event(
        self,
        event_type: str,
        key: str | None,
        timeout: timedelta | None,
        annotation: Any,
    ) -> Any:
        """Durably wait for an external event; hit returns it/resolves the timeout (V3)."""
        ...


#: The engine driving the current workflow, or ``None`` outside a drive.
CURRENT_DRIVER: ContextVar[Driver | None] = ContextVar("satay_current_driver", default=None)
