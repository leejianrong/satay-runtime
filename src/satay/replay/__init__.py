"""Replay engine, identity resolver, and nondeterminism detection (A2, N6/N7/N9).

Re-runs a workflow top-to-bottom on each drive, intercepts durable calls, resolves
identity by call-site ordinal plus task name (or explicit ``key=`` for fan-out), and
consults the journal: a hit returns the recorded result, a miss schedules execution.
Raises ``NondeterminismError`` on divergence. Pure Python asyncio, no external
dependency by design (ARCHITECTURE §3.2).

Sequential calls, hit/miss reconciliation, ``NondeterminismError`` enforcement under the
effect-safety policy, durable primitives, and keyed fan-out are all in place.
"""

from __future__ import annotations

from satay.replay.driver import CURRENT_DRIVER, Driver
from satay.replay.engine import ReplayEngine
from satay.replay.failures import TaskFailedError
from satay.replay.identity import CallIdentity, IdentityResolver


class NondeterminismError(RuntimeError):
    """Raised when a replay diverges from the recorded journal (N9, enforced from V2)."""


__all__ = [
    "CURRENT_DRIVER",
    "CallIdentity",
    "Driver",
    "IdentityResolver",
    "NondeterminismError",
    "ReplayEngine",
    "TaskFailedError",
]
