"""Runtime configuration and on-disk layout conventions.

Establishes the persistence-layout convention from ADR-0017: durable state lives
under a project-local ``./.satay/`` directory, overridable with ``--data-dir`` (or
the ``SATAY_DATA_DIR`` environment variable). The SQLite database and blob-spill
directory live inside it. This module owns only the path and mode conventions; the
live schema, its ``PRAGMA user_version`` migrations, and the connection settings live
in :mod:`satay.journal.store`.
"""

from __future__ import annotations

import os
from enum import StrEnum
from pathlib import Path

#: Name of the project-local data directory (ADR-0017).
DEFAULT_DATA_DIR_NAME = ".satay"

#: Environment variable that overrides the data directory.
DATA_DIR_ENV_VAR = "SATAY_DATA_DIR"

#: Environment variable providing the project-level effect-safety mode (ADR-0006).
EFFECT_SAFETY_ENV_VAR = "SATAY_EFFECT_SAFETY"

#: Environment variable providing the project-level nondeterminism policy (ADR-0022).
NONDETERMINISM_ENV_VAR = "SATAY_NONDETERMINISM"

#: Environment variable providing the project-level version-mismatch policy (ADR-0023).
VERSION_MISMATCH_ENV_VAR = "SATAY_VERSION_MISMATCH"


def _parse_mode[ModeT: StrEnum](cls: type[ModeT], value: str | ModeT, *, setting: str) -> ModeT:
    """Coerce ``value`` to a member of ``cls``, case- and whitespace-insensitively.

    Shared by the policy enums below, which have identical ``off``/``warn``/``strict``
    vocabularies but different scopes and defaults. Each caller resolves its own default,
    so ``None`` never reaches here. Raises :class:`ValueError` naming ``setting`` and the
    valid modes.
    """
    if isinstance(value, cls):
        return value
    try:
        return cls(str(value).strip().lower())
    except ValueError:
        valid = ", ".join(m.value for m in cls)
        raise ValueError(f"unknown {setting} mode {value!r}; expected one of: {valid}") from None


class EffectSafety(StrEnum):
    """Project effect-safety mode — **unguarded side effects only** (A10.2, ADR-0006).

    Governs exactly one check: a retryable ``side_effect=True`` task that declares no
    idempotency or compensation strategy. In :attr:`STRICT` the runtime rejects it at
    schedule time; :attr:`WARN` (the dev default) logs the same condition; :attr:`OFF`
    is silent.

    It does **not** govern replay divergence — that is :class:`NondeterminismPolicy`,
    which defaults to ``strict`` (ADR-0022) — and it does **not** govern the code-version
    mismatch check on resume — that is :class:`VersionMismatchPolicy` (ADR-0023). All
    three share an ``off``/``warn``/``strict`` vocabulary but not a risk profile, so they
    do not share a knob.
    """

    OFF = "off"
    WARN = "warn"
    STRICT = "strict"

    @classmethod
    def parse(cls, value: str | EffectSafety | None) -> EffectSafety:
        """Parse a mode, defaulting to :attr:`WARN` (the dev default) when unset.

        Raises :class:`ValueError` naming the valid modes for an unknown value.
        """
        if value is None:
            return cls.WARN
        return _parse_mode(cls, value, setting="effect_safety")


class NondeterminismPolicy(StrEnum):
    """Project policy for **replay divergence** (N9, ADR-0003/ADR-0022).

    Applies when a replayed durable call's task name does not match the journal at that
    position. In :attr:`STRICT` (the default) the runtime raises
    :class:`~satay.replay.nondeterminism.NondeterminismError`; :attr:`WARN` logs and
    lets the divergent call proceed as a fresh miss, which means **the run can complete
    with a wrong result**; :attr:`OFF` does the same silently.

    Strict is the default because a silently wrong answer is indistinguishable from a
    right one. ``warn`` and ``off`` are explicit opt-ins for local iteration, where
    editing a workflow body and watching what happens is the point.

    Distinct from :class:`EffectSafety`, which governs unguarded side effects and keeps
    its ``warn`` default.
    """

    OFF = "off"
    WARN = "warn"
    STRICT = "strict"

    @classmethod
    def parse(cls, value: str | NondeterminismPolicy | None) -> NondeterminismPolicy:
        """Parse a policy, defaulting to :attr:`STRICT` when unset.

        Raises :class:`ValueError` naming the valid modes for an unknown value.
        """
        if value is None:
            return cls.STRICT
        return _parse_mode(cls, value, setting="nondeterminism")


class VersionMismatchPolicy(StrEnum):
    """Project policy for **code-version mismatch on resume** (N17, ADR-0010/ADR-0023).

    Applies when a run is resumed by a process whose code version differs from the one
    stamped on the run at creation. In :attr:`STRICT` the runtime raises
    :class:`~satay.versioning.VersionMismatchError` and the resume is rejected;
    :attr:`WARN` (the default) logs and lets the resume proceed, pointing at a fork
    (ADR-0004) as the supported way to continue under new code; :attr:`OFF` is silent.

    ``warn`` is the default because it is what the runtime already did when this check
    rode on ``effect_safety``; ADR-0023 made the coupling explicit without changing the
    behaviour. Unlike a replay divergence, a version change is not by itself evidence
    that anything has diverged — the edit may not touch the workflow's durable calls at
    all, and any divergence it *does* cause is caught by
    :class:`NondeterminismPolicy` on its own terms.

    Distinct from :class:`EffectSafety` and :class:`NondeterminismPolicy`: same three
    mode names, three unrelated questions.
    """

    OFF = "off"
    WARN = "warn"
    STRICT = "strict"

    @classmethod
    def parse(cls, value: str | VersionMismatchPolicy | None) -> VersionMismatchPolicy:
        """Parse a policy, defaulting to :attr:`WARN` when unset.

        Raises :class:`ValueError` naming the valid modes for an unknown value.
        """
        if value is None:
            return cls.WARN
        return _parse_mode(cls, value, setting="version_mismatch")


def resolve_effect_safety(override: str | EffectSafety | None = None) -> EffectSafety:
    """Resolve the effect-safety mode: explicit ``override`` then env var then default.

    The default is :attr:`EffectSafety.WARN` (the dev default, ADR-0006).
    """
    if override is not None:
        return EffectSafety.parse(override)
    return EffectSafety.parse(os.environ.get(EFFECT_SAFETY_ENV_VAR))


def resolve_nondeterminism(
    override: str | NondeterminismPolicy | None = None,
) -> NondeterminismPolicy:
    """Resolve the nondeterminism policy: explicit ``override`` then env var then default.

    The default is :attr:`NondeterminismPolicy.STRICT` (ADR-0022), so a divergent replay
    raises unless something explicitly opted out.
    """
    if override is not None:
        return NondeterminismPolicy.parse(override)
    return NondeterminismPolicy.parse(os.environ.get(NONDETERMINISM_ENV_VAR))


def resolve_version_mismatch(
    override: str | VersionMismatchPolicy | None = None,
) -> VersionMismatchPolicy:
    """Resolve the version-mismatch policy: explicit ``override`` then env var then default.

    The default is :attr:`VersionMismatchPolicy.WARN` (ADR-0023), which is what the check
    did while it read ``effect_safety``'s ``warn`` default.
    """
    if override is not None:
        return VersionMismatchPolicy.parse(override)
    return VersionMismatchPolicy.parse(os.environ.get(VERSION_MISMATCH_ENV_VAR))


#: Filename of the SQLite database inside the data directory.
DB_FILENAME = "satay.db"

#: Subdirectory holding spilled payload blobs (see :mod:`satay.blobs`).
BLOB_DIR_NAME = "blobs"

#: Superseded — do not read this as the schema version. The authoritative value is
#: ``satay.journal.store.SCHEMA_VERSION``, which the store migrates to via forward-only
#: steps keyed on SQLite ``PRAGMA user_version`` (ADR-0017). This constant is retained
#: only as the "empty data dir" sentinel that predates the store.
SCHEMA_USER_VERSION = 0


def resolve_data_dir(override: str | os.PathLike[str] | None = None) -> Path:
    """Resolve the data directory, honouring ``override`` then the env var then default.

    Does not create the directory; callers that write to it are responsible for
    creating it (there is nothing to persist until V1).
    """
    if override is not None:
        return Path(override)
    env_value = os.environ.get(DATA_DIR_ENV_VAR)
    if env_value:
        return Path(env_value)
    return Path.cwd() / DEFAULT_DATA_DIR_NAME


def db_path(data_dir: Path) -> Path:
    """Return the SQLite database path within ``data_dir``."""
    return data_dir / DB_FILENAME


def blob_dir(data_dir: Path) -> Path:
    """Return the blob-spill directory within ``data_dir``."""
    return data_dir / BLOB_DIR_NAME
