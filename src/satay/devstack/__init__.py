"""The ``satay dev`` orchestrator (A9, N20).

Boots the worker (its asyncio loop) plus the control API (its own thread) and serves
Studio, and acquires an **exclusive advisory lock** on a lockfile in the data
directory so a second ``satay dev`` on the same ``./.satay/`` is refused rather than
racing the journal into corruption (ADR-0017 Q54).

``satay dev`` and its Typer command surface live **only in the ``satay[studio]``
extra** (ADR-0016); the core CLI does not wire it up. Scaffold only: the orchestrator
lands in V8.
"""

from __future__ import annotations
