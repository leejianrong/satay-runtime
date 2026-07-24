"""Durable-call identity resolver (N7, ADR-0002).

Identity for an ordinary durable call is the pair ``(task_name, ordinal)``: the Nth
durable call of task ``T`` during a drive maps to the Nth recorded entry for ``T``.
The resolver keeps a per-``task_name`` counter, bumped on each call seen during a
drive; a fresh resolver is created per drive so ordinals restart from 0.

**Fan-out identity (V4).** A dynamic ``satay.map`` item has no stable ordinal (item
count and completion order vary), so each item is identified by an explicit
``(task_name, key)`` instead, where ``key`` is a caller-supplied stable id per item
(ADR-0002). A keyed identity carries ``key`` and ignores ``ordinal``; the two forms
never collide because a keyed identity's ``ordinal`` stays at the ``-1`` sentinel.
Keyed identities resolve **independently of the ordinal counter**, so inserting or
reordering ordinary calls never shifts a map item's identity.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

#: Field separator for the idempotency-key pre-image (a byte that cannot occur in the
#: components, so the derivation is unambiguous).
_KEY_SEP = "\x00"

#: The ``ordinal`` sentinel a keyed (fan-out) identity carries in place of an ordinal.
_NO_ORDINAL = -1


@dataclass(frozen=True, slots=True)
class CallIdentity:
    """The durable identity of one call site.

    Either an ordinal identity ``(task_name, ordinal)`` for an ordinary durable call,
    or a keyed identity ``(task_name, key)`` for a ``satay.map`` item / keyed child
    (ADR-0002). A keyed identity leaves ``ordinal`` at the ``-1`` sentinel.
    """

    task_name: str
    ordinal: int = _NO_ORDINAL
    key: str | None = None

    @property
    def is_keyed(self) -> bool:
        """Whether this is a keyed (fan-out) identity rather than an ordinal one."""
        return self.key is not None

    @property
    def key_component(self) -> int | str:
        """The component fed to :func:`idempotency_key` — the map key, else the ordinal."""
        return self.key if self.key is not None else self.ordinal

    def payload_fields(self) -> dict[str, Any]:
        """The identity fields recorded on a journal event (``key`` xor ``ordinal``)."""
        if self.key is not None:
            return {"task_name": self.task_name, "key": self.key}
        return {"task_name": self.task_name, "ordinal": self.ordinal}

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> CallIdentity:
        """Reconstruct an identity from a recorded event payload (inverse of the above)."""
        if "key" in payload:
            return cls(task_name=payload["task_name"], key=payload["key"])
        return cls(task_name=payload["task_name"], ordinal=int(payload["ordinal"]))


def idempotency_key(run_id: str, task_name: str, ordinal_or_map_key: int | str) -> str:
    """Derive the stable idempotency key of a logical durable call (A4.3, ADR-0006).

    ``key = hash(run_id, task_name, ordinal_or_map_key)`` — deliberately excluding task
    *arguments* so it is **stable across physical retries** of the same logical task and
    **distinct across invocations** (a different ordinal, task, run, or **map key**
    yields a different key). ``ordinal_or_map_key`` is the per-name ordinal for ordinary
    calls and the explicit fan-out map key for a ``satay.map`` item. Exposed read-only to
    task bodies via ``ctx.idempotency_key``.
    """
    pre_image = _KEY_SEP.join((run_id, task_name, str(ordinal_or_map_key)))
    return hashlib.sha256(pre_image.encode("utf-8")).hexdigest()


def resolve_map_keys(
    items: Iterable[Any], key_fn: Callable[[Any], str] | None
) -> list[tuple[Any, str]]:
    """Pair each ``satay.map`` item with its explicit key, validated at schedule time.

    Enforces the ADR-0002 requirements before any item is scheduled: ``key=`` must be
    supplied, every item must yield a key, and keys must be unique within one ``map``.
    A violation is a usage error raised (``ValueError``) before any durable call runs.
    """
    if key_fn is None:
        raise ValueError(
            "satay.map requires key= : a callable mapping each item to a stable, "
            "unique string id (ADR-0002 — fan-out has no stable ordinal)"
        )
    pairs: list[tuple[Any, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        key = key_fn(item)
        if key is None or key == "":
            raise ValueError(
                f"satay.map: item at index {index} has no key; key= must return a "
                f"non-empty stable id for every item (ADR-0002)"
            )
        if not isinstance(key, str):
            raise ValueError(
                f"satay.map: key for item at index {index} is {type(key).__name__!r}, "
                f"not str; keys must be strings (ADR-0002)"
            )
        if key in seen:
            raise ValueError(
                f"satay.map: duplicate item key {key!r}; keys must be unique within one "
                f"map so each item has a distinct durable identity (ADR-0002)"
            )
        seen.add(key)
        pairs.append((item, key))
    return pairs


class IdentityResolver:
    """Allocates sequential per-task-name ordinals across a single run-drive."""

    def __init__(self) -> None:
        self._counters: dict[str, int] = defaultdict(int)

    def next(self, task_name: str) -> CallIdentity:
        """Return the next ``(task_name, ordinal)`` identity for ``task_name``."""
        ordinal = self._counters[task_name]
        self._counters[task_name] = ordinal + 1
        return CallIdentity(task_name=task_name, ordinal=ordinal)
