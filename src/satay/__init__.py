"""Satay Runtime — local-first durable execution for async Python.

Early scaffold (Epic 0): the public surface below is declared and typed, but most
behaviour raises ``NotImplementedError`` with a "lands in Vn" note until the slice
that implements it. Trust the code over the docs. See ``CLAUDE.md`` and ``docs/``.

The public surface (ARCHITECTURE §1):

- ``@workflow`` / ``@task`` — author decorators
- ``start`` — create/look up a run, returns a ``RunHandle``
- ``sleep`` / ``wait_for_event`` / ``send_event`` — event & time primitives
- ``map`` / ``gather`` / ``start_child`` — composition primitives
- ``TaskContext`` — the task-body context
- ``RunHandle`` — the run handle
"""

from __future__ import annotations

from satay.api import (
    EffectSafetyError,
    NondeterminismError,
    RunHandle,
    TaskContext,
    gather,
    map,
    send_event,
    sleep,
    start,
    start_child,
    task,
    task_context,
    wait_for_event,
    workflow,
)
from satay.versioning import VersionMismatchError

__version__ = "0.0.0"

__all__ = [
    "EffectSafetyError",
    "NondeterminismError",
    "RunHandle",
    "TaskContext",
    "VersionMismatchError",
    "__version__",
    "gather",
    "map",
    "send_event",
    "sleep",
    "start",
    "start_child",
    "task",
    "task_context",
    "wait_for_event",
    "workflow",
]
