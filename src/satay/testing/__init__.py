"""First-class test-seam affordances (ADR-0011).

This module is a **first-class runtime module, not a test helper**: the fault-injection
hook, the injectable clock, and the seedable RNG are runtime affordances the executor
and journal depend on (real by default), that tests swap for deterministic variants.

Exported here (import-clean, no pytest dependency):

- ``Clock`` / ``RealClock`` / ``ManualClock`` — injectable time
- ``Rng`` / ``SystemRng`` / ``SeededRng`` — injectable randomness
- ``FaultInjector`` / ``SimulatedCrash`` — crash/stall injection
- ``settle`` / ``NeverSettledError`` — drive an awaitable under a ``ManualClock``

The pytest fixtures live in :mod:`satay.testing.fixtures` (a pytest plugin) and are not
imported here, so importing ``satay.testing`` never requires pytest. ``settle`` is a plain
function rather than a fixture for the same reason a script is not a test: the
``examples/`` demos need it too.
"""

from __future__ import annotations

from satay.testing.clock import Clock, ManualClock, RealClock
from satay.testing.faults import FaultInjector, SimulatedCrash
from satay.testing.rng import Rng, SeededRng, SystemRng
from satay.testing.settle import NeverSettledError, settle

__all__ = [
    "Clock",
    "FaultInjector",
    "ManualClock",
    "NeverSettledError",
    "RealClock",
    "Rng",
    "SeededRng",
    "SimulatedCrash",
    "SystemRng",
    "settle",
]
