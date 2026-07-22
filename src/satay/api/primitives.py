"""The durable primitives and ``satay.start``.

Every function here is a durable call routed through the replay engine, except
``start`` (creates/looks up a run) and ``send_event`` (a control-plane write). All
are public surface; behaviour lands in the slice noted on each.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from datetime import timedelta
from typing import Any

from satay.api.run_handle import RunHandle


def start(
    workflow: Callable[..., Awaitable[Any]],
    workflow_input: Any = None,
    *,
    idempotency_key: str | None = None,
) -> RunHandle:
    """Create or look up a run and return its handle (N3, lands in V1)."""
    raise NotImplementedError("satay.start lands in V1")


async def sleep(duration: float | timedelta) -> None:
    """Durably sleep for ``duration`` (N5, lands in V3)."""
    raise NotImplementedError("satay.sleep lands in V3")


async def wait_for_event(
    name: str,
    *,
    timeout: float | timedelta | None = None,
) -> Any:
    """Durably wait for a named external event, optionally with a timeout (N5, lands in V3)."""
    raise NotImplementedError("satay.wait_for_event lands in V3")


def send_event(run_id: str, name: str, payload: Any = None) -> None:
    """Deliver a named external event to a run (control-plane write, lands in V3)."""
    raise NotImplementedError("satay.send_event lands in V3")


async def map(
    task: Callable[..., Awaitable[Any]],
    items: Iterable[Any],
    *,
    key: Callable[[Any], str] | None = None,
) -> list[Any]:
    """Durable fan-out of ``task`` over ``items``; fail-fast (N5, D21, lands in V4)."""
    raise NotImplementedError("satay.map lands in V4")


async def gather(*awaitables: Awaitable[Any]) -> list[Any]:
    """Durably await several durable calls concurrently; fail-fast (N5, D21, lands in V4)."""
    raise NotImplementedError("satay.gather lands in V4")


async def start_child(
    workflow: Callable[..., Awaitable[Any]],
    workflow_input: Any = None,
    *,
    key: str | None = None,
) -> RunHandle:
    """Start a durable child workflow (N5, lands in V4)."""
    raise NotImplementedError("satay.start_child lands in V4")
