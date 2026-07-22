"""Author-facing API surface (A1): decorators, primitives, run handle, TaskContext.

This is the boundary the developer imports. It looks like ordinary async Python
while routing every durable call through the replay engine (ARCHITECTURE §3.1).
"""

from __future__ import annotations

from satay.api.context import TaskContext, task_context
from satay.api.decorators import task, workflow
from satay.api.primitives import (
    gather,
    map,
    send_event,
    sleep,
    start,
    start_child,
    wait_for_event,
)
from satay.api.run_handle import RunHandle
from satay.replay.nondeterminism import EffectSafetyError, NondeterminismError

__all__ = [
    "EffectSafetyError",
    "NondeterminismError",
    "RunHandle",
    "TaskContext",
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
