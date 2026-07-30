"""Satay Runtime — local-first durable execution for async Python.

The public surface below is implemented and exercised end-to-end: durable calls are
recorded to a SQLite journal and workflows replay from the top on resume, reusing
recorded results. Async workflows and tasks only. See ``CLAUDE.md`` for the shipped
feature set and the deliberate MVP gaps, and ``docs/`` for the specs.

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
