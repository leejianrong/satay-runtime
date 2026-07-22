"""Author-facing decorators: ``@satay.workflow`` and ``@satay.task``.

Pure Python, no third-party dependency (ARCHITECTURE §3.1). These register the
decorated coroutine and wrap calls to route through the replay engine. Behaviour
lands in V1; decorating raises until then.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

_AsyncFn = TypeVar("_AsyncFn", bound=Callable[..., Awaitable[Any]])


def workflow[AsyncFnT: Callable[..., Awaitable[Any]]](fn: AsyncFnT) -> AsyncFnT:
    """Register a workflow definition and wrap it to drive replay (N1, lands in V1)."""
    raise NotImplementedError("@satay.workflow lands in V1")


def task(
    *,
    retries: int = 0,
    timeout: float | None = None,
    side_effect: bool = False,
) -> Callable[[_AsyncFn], _AsyncFn]:
    """Register a task and wrap calls as durable calls (N2).

    ``retries``/``timeout``/``side_effect`` are accepted now; the retry loop and
    effect-safety enforcement land in V2. Behaviour lands in V1.
    """
    raise NotImplementedError("@satay.task lands in V1")
