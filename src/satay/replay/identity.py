"""Durable-call identity resolver (N7, ADR-0002).

Identity for an ordinary durable call is the pair ``(task_name, ordinal)``: the Nth
durable call of task ``T`` during a drive maps to the Nth recorded entry for ``T``.
The resolver keeps a per-``task_name`` counter, bumped on each call seen during a
drive; a fresh resolver is created per drive so ordinals restart from 0. Explicit
``key=`` for fan-out is V4.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CallIdentity:
    """The durable identity of one call site: task name plus its per-name ordinal."""

    task_name: str
    ordinal: int


class IdentityResolver:
    """Allocates sequential per-task-name ordinals across a single run-drive."""

    def __init__(self) -> None:
        self._counters: dict[str, int] = defaultdict(int)

    def next(self, task_name: str) -> CallIdentity:
        """Return the next ``(task_name, ordinal)`` identity for ``task_name``."""
        ordinal = self._counters[task_name]
        self._counters[task_name] = ordinal + 1
        return CallIdentity(task_name=task_name, ordinal=ordinal)
