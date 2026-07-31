"""Importing the user's workflow modules so ``satay dev`` can run them (KAN-448).

The registry (:data:`satay.api.registry.REGISTRY`) is populated purely as a side effect
of ``@satay.workflow`` / ``@satay.task`` decorators executing at **import time**. A
standalone ``satay dev`` imports none of the user's code, so its registry is empty and
its worker cannot resolve a workflow by name in order to wake a parked run — the poll
loop turns, and nothing ever fires. This module is the missing step: it imports the
modules named by ``--app`` (or by ``[tool.satay] app`` in ``pyproject.toml``) *before*
the stack boots, and reports exactly what ended up registered.

Two design rules, both about the silence that made the bug invisible:

- **Nothing fails quietly.** A module that does not exist, that is not importable from
  here, or that raises on import produces an :class:`AppImportError` naming the module
  and the underlying cause. ``satay dev`` prints it and exits non-zero.
- **The boot always says what it can run.** Even the no-``--app`` case prints
  ``0 workflows`` plus the reason, rather than looking like a healthy stack.

Everything here is stdlib (``importlib`` + ``tomllib``), so it does not widen the
core-dependency boundary (ADR-0016); it lives in ``devstack`` because only
``satay dev`` needs it.
"""

from __future__ import annotations

import importlib
import re
import sys
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from satay.api.registry import REGISTRY

#: The config file consulted when no ``--app`` is passed.
CONFIG_FILENAME = "pyproject.toml"

#: The table and key holding the default module list: ``[tool.satay] app = [...]``.
CONFIG_TABLE = "tool.satay"
CONFIG_KEY = "app"

#: A dotted Python module path — deliberately not a file path (see :func:`_import_one`).
_MODULE_PATH_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")


class AppImportError(RuntimeError):
    """An ``--app`` module could not be resolved, or raised while importing.

    Carries a message naming the module and the underlying cause; ``satay dev`` prints
    it verbatim and exits non-zero rather than booting with an empty registry.
    """


@dataclass(frozen=True, slots=True)
class AppLoadReport:
    """What ``satay dev`` imported and what the process can consequently run."""

    #: The module paths that were imported, in the order given.
    modules: tuple[str, ...]
    #: Where the module list came from: ``"--app"``, the config file, or ``"none"``.
    source: str
    #: Every workflow name in the registry after the import (not just the new ones).
    workflows: tuple[str, ...]
    #: Every task name in the registry after the import.
    tasks: tuple[str, ...]

    @property
    def can_run_workflows(self) -> bool:
        """Whether this process has *any* workflow it could start or wake."""
        return bool(self.workflows)

    def describe(self) -> list[str]:
        """Human-readable boot lines: what was imported, and what is registered."""
        if self.modules:
            lines = [f"app modules ({self.source}): {', '.join(self.modules)}"]
        else:
            lines = [
                "app modules: none "
                f"(no --app, no [{CONFIG_TABLE}] {CONFIG_KEY} in {CONFIG_FILENAME})"
            ]
        lines.append(
            f"registered: {_count(self.workflows, 'workflow')}; {_count(self.tasks, 'task')}"
        )
        if not self.can_run_workflows:
            lines.append(
                "  warning: 0 workflows registered — this process can serve Studio and read "
                "the journal, but it cannot start a run or wake one parked on a timer or "
                "event. Pass --app your.module to import your workflows."
            )
        return lines


def _count(names: Sequence[str], noun: str) -> str:
    """``2 workflows (a, b)`` / ``0 tasks`` — a count that always states the number."""
    plural = noun if len(names) == 1 else f"{noun}s"
    if not names:
        return f"0 {plural}"
    return f"{len(names)} {plural} ({', '.join(names)})"


def load_app(
    modules: Sequence[str] | None = None,
    *,
    project_dir: Path | str | None = None,
) -> AppLoadReport:
    """Resolve and import the user's app modules; return what got registered.

    ``modules`` (from ``--app``) wins outright when non-empty; otherwise the
    ``[tool.satay] app`` key of ``pyproject.toml`` in ``project_dir`` (default: the
    current directory) supplies the list. Raises :class:`AppImportError` for a bad
    config value or any module that fails to import.
    """
    root = Path(project_dir) if project_dir is not None else Path.cwd()
    resolved, source = resolve_app_modules(modules, project_dir=root)
    if resolved:
        _ensure_project_importable(root)
        for name in resolved:
            _import_one(name)
    return AppLoadReport(
        modules=resolved,
        source=source,
        workflows=tuple(REGISTRY.workflow_names()),
        tasks=tuple(REGISTRY.task_names()),
    )


def resolve_app_modules(
    modules: Sequence[str] | None,
    *,
    project_dir: Path,
) -> tuple[tuple[str, ...], str]:
    """Return ``(module paths, where they came from)`` without importing anything."""
    explicit = tuple(m for m in (modules or ()) if m)
    if explicit:
        return explicit, "--app"
    from_config = _modules_from_config(project_dir)
    if from_config:
        return from_config, str(project_dir / CONFIG_FILENAME)
    return (), "none"


def _modules_from_config(project_dir: Path) -> tuple[str, ...]:
    """Read ``[tool.satay] app`` from ``project_dir/pyproject.toml`` (stdlib ``tomllib``)."""
    path = project_dir / CONFIG_FILENAME
    if not path.is_file():
        return ()
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise AppImportError(f"could not read {path}: {exc}") from exc

    table = data.get("tool")
    section = table.get("satay") if isinstance(table, dict) else None
    if not isinstance(section, dict):
        return ()
    value = section.get(CONFIG_KEY)
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return tuple(value)
    raise AppImportError(
        f"{path}: [{CONFIG_TABLE}] {CONFIG_KEY} must be a module path or a list of "
        f"module paths, got {type(value).__name__}"
    )


def _ensure_project_importable(project_dir: Path) -> None:
    """**Append** the project directory to ``sys.path`` so ``--app`` can find local code.

    A console-script entry point (``satay``) does not put the working directory on
    ``sys.path`` the way ``python -m`` does, so ``--app mypkg.workflows`` would fail for
    any project that is not pip-installed — the common case for someone trying the
    runtime out.

    The entry is **appended, never prepended**: it lands after the stdlib and
    site-packages, so a stray ``queue.py`` or ``types.py`` in the project directory
    cannot shadow the stdlib module of that name. That is the whole reason this is not
    ``sys.path.insert(0, ...)``. It does mean a local package sharing a name with an
    installed one loses; that is the intended trade.
    """
    entry = str(project_dir.resolve())
    if entry not in sys.path:
        sys.path.append(entry)


def _import_one(name: str) -> None:
    """Import one ``--app`` module, converting every failure into an ``AppImportError``."""
    if not _MODULE_PATH_RE.match(name):
        hint = ""
        if name.endswith(".py") or "/" in name or "\\" in name:
            hint = " It looks like a file path; --app takes a dotted module path."
        raise AppImportError(f"--app {name!r} is not a valid Python module path.{hint}")

    try:
        importlib.import_module(name)
    except ModuleNotFoundError as exc:
        missing = exc.name or name
        if missing == name or name.startswith(f"{missing}."):
            raise AppImportError(
                f"--app module {name!r} was not found (no module named {missing!r}). "
                f"It must be importable from {Path.cwd()} — either installed into this "
                f"environment, or a package/module directory here."
            ) from exc
        raise AppImportError(
            f"--app module {name!r} failed to import: it imports {missing!r}, "
            f"which is not installed ({exc})."
        ) from exc
    except Exception as exc:
        raise AppImportError(
            f"--app module {name!r} raised {type(exc).__name__} while importing: {exc}"
        ) from exc


__all__ = [
    "CONFIG_FILENAME",
    "CONFIG_KEY",
    "CONFIG_TABLE",
    "AppImportError",
    "AppLoadReport",
    "load_app",
    "resolve_app_modules",
]
