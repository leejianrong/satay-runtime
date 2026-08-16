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
- ``TaskContext`` — the task-body context
- ``RunHandle`` — the run handle
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
from satay.api.fork import fork
from satay.api.run_handle import WorkflowFailedError
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
    "EffectSafetyError",
    "NondeterminismError",
    "RunHandle",
    "TaskContext",
    "VersionMismatchError",
    "WorkflowFailedError",
    "__version__",
    "fork",
    "gather",
    "map",
    "send_event",
    "sleep",
    "start",
    "start_child",
    "task",
    "task_context",
    "wait_for_event",
    "workflow",
]
