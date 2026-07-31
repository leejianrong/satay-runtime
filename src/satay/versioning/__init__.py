"""Code-version stamping and mismatch policy (A10, N17).

Stamps each run with a code version resolved once at creation, in fallback order:
a **git commit** (via the ``git`` binary — no ``dulwich``, ADR-0015), else a
developer-provided **dev string**, else a **source hash** of the registered
definitions. Recorded on ``WorkflowCreated`` (V1 stamped only).

**V7 turns the stamp into a policy (N17, ADR-0010).** On resume, the worker compares
the run's *stamped* version against the *current* one; on a mismatch it applies its
**own** :class:`~satay.config.VersionMismatchPolicy` — ``strict`` raises
:class:`VersionMismatchError` (the resume is rejected); ``warn`` (the default) logs and
continues, pointing at the fork as the offered path; ``off`` is silent. There is **no
automatic migration** (ADR-0010) — the developer forks (ADR-0004) to continue under new
code. The read API surfaces the mismatch so Studio can show a banner (ADR-0018).

The policy shares the ``off``/``warn``/``strict`` shape with ``effect_safety``
(ADR-0006) and the nondeterminism policy (ADR-0022) but is a **separate setting**
(ADR-0023): it used to read ``effect_safety``, so quieting a side-effect warning also
silently disabled version-mismatch rejection.
"""

from __future__ import annotations

import hashlib
import inspect
import logging
import os
import subprocess
from collections.abc import Awaitable, Callable
from typing import Any

from satay.config import VersionMismatchPolicy

_LOG = logging.getLogger("satay")

#: Environment variable providing an explicit dev version string (second fallback).
DEV_VERSION_ENV_VAR = "SATAY_CODE_VERSION"


class VersionMismatchError(RuntimeError):
    """Raised on resume under strict mode when the code version has changed (N17).

    Mirrors :class:`~satay.replay.nondeterminism.NondeterminismError` and
    :class:`~satay.replay.nondeterminism.EffectSafetyError`: a strict-mode policy
    failure the developer resolves (here, by forking to continue under new code —
    ADR-0004/0010), never an automatic migration.
    """

    def __init__(self, stamped: str, current: str) -> None:
        super().__init__(
            f"code version mismatch on resume: run was stamped {stamped!r} but the current "
            f"code version is {current!r}; strict mode rejects the resume — fork the run to "
            f"continue under the new code (ADR-0004/0010, no automatic migration)"
        )
        self.stamped = stamped
        self.current = current


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


def current_code_version(
    *,
    dev_string: str | None = None,
    source_targets: list[Callable[..., Awaitable[Any]]] | None = None,
) -> str:
    """The code version of the *running* process, resolved the same way as the stamp.

    A thin, intention-revealing alias of :func:`stamp_code_version` used by the resume
    check and the read API to compare a run's recorded version against "now". Kept as a
    separate name so callers (and tests) can target the current-version resolution
    without re-reading the whole stamping doc; not cached, so a fresh git commit is
    reflected immediately.
    """
    return stamp_code_version(dev_string=dev_string, source_targets=source_targets)


def is_version_mismatch(stamped: str, current: str) -> bool:
    """Whether a run's ``stamped`` version differs from the ``current`` one (N17)."""
    return stamped != current


def check_resume_version(stamped: str, current: str, policy: VersionMismatchPolicy) -> None:
    """Apply the version-mismatch policy on resume (N17, ADR-0010/0023).

    No mismatch is a no-op. On a mismatch: ``strict`` raises
    :class:`VersionMismatchError` (rejecting the resume); ``warn`` (the default) logs and
    returns — the resume proceeds, but the developer is pointed at forking; ``off`` is
    silent.

    ``policy`` is :class:`~satay.config.VersionMismatchPolicy`, **not**
    :class:`~satay.config.EffectSafety`. The shape is the same as effect-safety and
    nondeterminism enforcement; the question is not (ADR-0023).
    """
    if not is_version_mismatch(stamped, current):
        return
    if policy is VersionMismatchPolicy.STRICT:
        raise VersionMismatchError(stamped, current)
    if policy is VersionMismatchPolicy.WARN:
        _LOG.warning(
            "code version mismatch on resume: run stamped %s, current %s; resuming under "
            "changed code may diverge — consider forking to continue under the new code "
            "(ADR-0004/0010)",
            stamped,
            current,
        )
    # off: silent — the resume proceeds unremarked.


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
