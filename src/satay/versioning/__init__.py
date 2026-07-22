"""Code-version stamping and mismatch policy (A10, N17).

Stamps each run with a code version resolved once at creation, in fallback order:
a **git commit** (via the ``git`` binary — no ``dulwich``, ADR-0015), else a
developer-provided **dev string**, else a **source hash** of the registered
definitions. Recorded on ``WorkflowCreated`` so V7 has the data; **V1 stamps only** —
the mismatch check on resume is V7.
"""

from __future__ import annotations

import hashlib
import inspect
import os
import subprocess
from collections.abc import Awaitable, Callable
from typing import Any

#: Environment variable providing an explicit dev version string (second fallback).
DEV_VERSION_ENV_VAR = "SATAY_CODE_VERSION"


def stamp_code_version(
    *,
    dev_string: str | None = None,
    source_targets: list[Callable[..., Awaitable[Any]]] | None = None,
) -> str:
    """Resolve the current code version for a new run (N17, stamp-only).

    Order: git commit → dev string (arg or ``SATAY_CODE_VERSION``) → source hash of
    the registered definitions. Never raises: the source hash is always available.
    """
    commit = _git_commit()
    if commit is not None:
        return f"git:{commit}"

    dev = dev_string or os.environ.get(DEV_VERSION_ENV_VAR)
    if dev:
        return f"dev:{dev}"

    if source_targets is None:
        from satay.api.registry import REGISTRY

        source_targets = REGISTRY.iter_source_targets()
    return f"src:{_source_hash(source_targets)}"


def _git_commit() -> str | None:
    """Return the current git commit hash, or ``None`` if unavailable."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    commit = result.stdout.strip()
    return commit or None


def _source_hash(targets: list[Callable[..., Awaitable[Any]]]) -> str:
    """Hash the source of the registered definitions (deterministic, order-stable)."""
    hasher = hashlib.sha256()
    for fn in targets:
        try:
            src = inspect.getsource(fn)
        except (OSError, TypeError):
            src = f"{fn.__module__}.{getattr(fn, '__qualname__', fn.__name__)}"
        hasher.update(src.encode("utf-8"))
        hasher.update(b"\0")
    return hasher.hexdigest()[:16]
