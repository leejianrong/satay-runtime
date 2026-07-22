"""Replay engine, identity resolver, and nondeterminism detection (A2, N6/N7/N9).

Re-runs a workflow top-to-bottom on each drive, intercepts durable calls, resolves
identity by call-site ordinal plus task name (or explicit ``key=`` for fan-out), and
consults the journal: a hit returns the recorded result, a miss schedules execution.
Raises ``NondeterminismError`` on divergence. Pure Python asyncio, no external
dependency by design (ARCHITECTURE §3.2).

Scaffold only: the engine lands in V1, nondeterminism enforcement in V2, fan-out in V4.
"""

from __future__ import annotations


class NondeterminismError(RuntimeError):
    """Raised when a replay diverges from the recorded journal (N9, enforced from V2)."""
