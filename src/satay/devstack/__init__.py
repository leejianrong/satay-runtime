"""The ``satay dev`` orchestrator (A9, N20).

Boots the worker (its asyncio loop), the SQLite store (with blob spill), the control +
read HTTP API, and the served Studio SPA in **one process**, and takes an **exclusive
advisory lock** on a lockfile in the data directory so a second ``satay dev`` on the same
``./.satay/`` is refused rather than racing the journal into corruption (ADR-0017/Q54).

``satay dev`` and its Typer command surface live **only in the ``satay[studio]`` extra**
(ADR-0016). This package ``__init__`` stays import-clean — it pulls in **no**
FastAPI/uvicorn/Typer at load (the import-hygiene guard imports ``satay.devstack`` in a
bare interpreter) — so the orchestrator and Typer command are reached through the lazy
accessors below only when ``satay dev`` actually runs.
"""

from __future__ import annotations

from satay.devstack.lock import DataDirLock, DataDirLockedError


def run_dev(**kwargs: object) -> int:
    """Boot the dev stack (studio-only; imports FastAPI/uvicorn lazily). See orchestrator."""
    from satay.devstack.orchestrator import run_dev as _run_dev

    return _run_dev(**kwargs)  # type: ignore[arg-type]


__all__ = ["DataDirLock", "DataDirLockedError", "run_dev"]
