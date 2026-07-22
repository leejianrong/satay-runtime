"""Durable-call identity resolver (N7, ADR-0002).

Identity for an ordinary durable call is the pair ``(task_name, ordinal)``: the Nth
durable call of task ``T`` during a drive maps to the Nth recorded entry for ``T``.
The resolver keeps a per-``task_name`` counter, bumped on each call seen during a
drive; a fresh resolver is created per drive so ordinals restart from 0. Explicit
``key=`` for fan-out is V4.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass

#: Field separator for the idempotency-key pre-image (a byte that cannot occur in the
#: components, so the derivation is unambiguous).
_KEY_SEP = "\x00"


@dataclass(frozen=True, slots=True)
class CallIdentity:
    """The durable identity of one call site: task name plus its per-name ordinal."""

    task_name: str
    ordinal: int


def idempotency_key(run_id: str, task_name: str, ordinal_or_map_key: int | str) -> str:
    """Derive the stable idempotency key of a logical durable call (A4.3, ADR-0006).

    ``key = hash(run_id, task_name, ordinal_or_map_key)`` — deliberately excluding task
    *arguments* so it is **stable across physical retries** of the same logical task and
    **distinct across invocations** (a different ordinal, task, or run yields a different
    key). ``ordinal_or_map_key`` is the per-name ordinal today; V4 passes an explicit
    fan-out map key here. Exposed read-only to task bodies via ``ctx.idempotency_key``.
    """
    pre_image = _KEY_SEP.join((run_id, task_name, str(ordinal_or_map_key)))
    return hashlib.sha256(pre_image.encode("utf-8")).hexdigest()


class IdentityResolver:
    """Allocates sequential per-task-name ordinals across a single run-drive."""

    def __init__(self) -> None:
        self._counters: dict[str, int] = defaultdict(int)

    def next(self, task_name: str) -> CallIdentity:
        """Return the next ``(task_name, ordinal)`` identity for ``task_name``."""
        ordinal = self._counters[task_name]
        self._counters[task_name] = ordinal + 1
        return CallIdentity(task_name=task_name, ordinal=ordinal)
