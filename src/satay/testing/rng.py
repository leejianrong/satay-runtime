"""Injectable randomness (ADR-0011, Q46).

Backoff jitter (V2) is runtime randomness that the manual clock does not pin. The
runtime therefore depends on an injectable, seedable RNG so backoff schedules are
exactly reproducible under test. ``SystemRng`` is the production default (real
entropy); ``SeededRng`` is deterministic given a seed. Both satisfy ``Rng``.
"""

from __future__ import annotations

import random
from typing import Protocol, runtime_checkable


@runtime_checkable
class Rng(Protocol):
    """The randomness seam depended on by retry/backoff (ADR-0011)."""

    def random(self) -> float:
        """Return a float in [0.0, 1.0)."""
        ...

    def uniform(self, low: float, high: float) -> float:
        """Return a float in [low, high]."""
        ...


class SystemRng:
    """System-entropy RNG. The production default (non-reproducible)."""

    def __init__(self) -> None:
        self._rng = random.Random()

    def random(self) -> float:
        return self._rng.random()

    def uniform(self, low: float, high: float) -> float:
        return self._rng.uniform(low, high)


class SeededRng:
    """Deterministic RNG seeded reproducibly. Use in tests to pin jitter."""

    def __init__(self, seed: int) -> None:
        self._seed = seed
        self._rng = random.Random(seed)

    @property
    def seed(self) -> int:
        return self._seed

    def reset(self) -> None:
        """Restore the RNG to its initial seeded state."""
        self._rng = random.Random(self._seed)

    def random(self) -> float:
        return self._rng.random()

    def uniform(self, low: float, high: float) -> float:
        return self._rng.uniform(low, high)
