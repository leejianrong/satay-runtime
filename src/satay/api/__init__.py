"""Author-facing API surface (A1): decorators, primitives, run handle, TaskContext.

This is the boundary the developer imports. It looks like ordinary async Python
while routing every durable call through the replay engine (ARCHITECTURE §3.1).
"""

from __future__ import annotations

from satay.api.context import TaskContext
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

__all__ = [
    "RunHandle",
    "TaskContext",
    "gather",
    "map",
    "send_event",
    "sleep",
    "start",
    "start_child",
    "task",
    "wait_for_event",
    "workflow",
]
