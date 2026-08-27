"""Satay Runtime — local-first durable execution for async Python.

The public surface below is implemented and exercised end-to-end: durable calls are
recorded to a SQLite journal and workflows replay from the top on resume, reusing
recorded results. Async workflows and tasks only. See ``CLAUDE.md`` for the shipped
feature set and the deliberate MVP gaps, and ``docs/`` for the specs.

The public surface (ARCHITECTURE §1):

- ``@workflow`` / ``@task`` — author decorators
- ``start`` — create/look up a run, returns a ``RunHandle``
- ``sleep`` / ``wait_for_event`` / ``send_event`` — event & time primitives
- ``map`` / ``gather`` / ``start_child`` — composition primitives
- ``fork`` — re-cut a finished run from a chosen point, optionally under a new input
- ``inspect`` — read back a run's recorded durable calls, without forking it
- ``diff`` — compare two runs call by call, and see where their values differ
- ``run_app`` — ``async with``: the journal open and the poll loop running
- ``TaskContext`` — the task-body context
- ``RunHandle`` — the run handle
- ``RunStatus`` — what ``RunHandle.status()`` returns; a ``StrEnum``
- ``RunInspection`` / ``RecordedCall`` — what ``inspect`` returns
- ``RunDiff`` / ``CallDiff`` / ``ValueDiff`` — what ``diff`` returns
- ``PARKED`` — what ``result()`` returns for a run parked with nothing to wake it
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _distribution_version
from pathlib import Path

from satay.api import (
    EffectSafetyError,
    NondeterminismError,
    RunHandle,
    TaskContext,
    gather,
    map,
    send_event,
    sleep,
    start,
    start_child,
    task,
    task_context,
    wait_for_event,
    workflow,
)
from satay.api.app import run_app
from satay.api.diffing import CallDiff, RunDiff, ValueDiff, diff
from satay.api.fork import fork
from satay.api.inspection import RecordedCall, RunInspection, inspect
from satay.api.run_handle import PARKED, WorkflowFailedError

# `RunHandle.status()` returns a `RunStatus`, so the type has to be reachable from the
# public package: a user should not have to import out of `satay.journal.events` to name
# the value a public method just handed them (KAN-524).
from satay.journal.events import RunStatus
from satay.replay.failures import TaskFailedError
from satay.versioning import VersionMismatchError


def _version_from_source_tree() -> str | None:
    """Read ``project.version`` out of the checkout's ``pyproject.toml``.

    This is the fallback for a source tree whose distribution is not installed, so a
    contributor running straight off ``src/`` still sees the real version instead of a
    placeholder. Returns ``None`` when this file is not sitting in a Satay checkout —
    which is the normal installed case, where the metadata above already answered.
    """
    import tomllib

    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    try:
        with pyproject.open("rb") as handle:
            data: dict[str, object] = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    project = data.get("project")
    if not isinstance(project, dict) or project.get("name") != "satay":
        return None
    declared = project.get("version")
    return declared if isinstance(declared, str) else None


def _detect_version() -> str:
    """Resolve the package version from metadata, never from a constant in this file.

    ``pyproject.toml`` is the single source of truth: the installed distribution's
    metadata is generated from it at build time, and the source-tree fallback reads it
    directly. Hard-coding the version here is what shipped ``0.0.0`` in ``0.1.0a1``
    (KAN-447) — the value must not be able to drift from ``pyproject.toml`` again.
    """
    try:
        return _distribution_version("satay")
    except PackageNotFoundError:
        # Not installed: a bare source checkout, or an import off a path entry.
        return _version_from_source_tree() or "0.0.0.dev0+unknown"


__version__: str = _detect_version()

__all__ = [
    "PARKED",
    "CallDiff",
    "EffectSafetyError",
    "NondeterminismError",
    "RecordedCall",
    "RunDiff",
    "RunHandle",
    "RunInspection",
    "RunStatus",
    "TaskContext",
    "TaskFailedError",
    "ValueDiff",
    "VersionMismatchError",
    "WorkflowFailedError",
    "__version__",
    "diff",
    "fork",
    "gather",
    "inspect",
    "map",
    "run_app",
    "send_event",
    "sleep",
    "start",
    "start_child",
    "task",
    "task_context",
    "wait_for_event",
    "workflow",
]
