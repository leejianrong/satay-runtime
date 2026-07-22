"""Runtime configuration and on-disk layout conventions.

Establishes the persistence-layout convention from ADR-0017: durable state lives
under a project-local ``./.satay/`` directory, overridable with ``--data-dir`` (or
the ``SATAY_DATA_DIR`` environment variable). The SQLite database and blob-spill
directory live inside it. No database or schema exists yet (that lands in V1); this
module only fixes the paths and the migration-version convention so later slices
agree on where things go.
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


class EffectSafety(StrEnum):
    """Project effect-safety mode (A10.2, ADR-0006).

    In :attr:`STRICT`, a retryable ``side_effect=True`` task must declare an
    idempotency or compensation strategy or the runtime rejects it at schedule time.
    :attr:`WARN` (the dev default) logs the same condition; :attr:`OFF` is silent.
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
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).strip().lower())
        except ValueError:
            valid = ", ".join(m.value for m in cls)
            raise ValueError(
                f"unknown effect_safety mode {value!r}; expected one of: {valid}"
            ) from None


def resolve_effect_safety(override: str | EffectSafety | None = None) -> EffectSafety:
    """Resolve the effect-safety mode: explicit ``override`` then env var then default.

    The default is :attr:`EffectSafety.WARN` (the dev default, ADR-0006).
    """
    if override is not None:
        return EffectSafety.parse(override)
    return EffectSafety.parse(os.environ.get(EFFECT_SAFETY_ENV_VAR))


#: Filename of the SQLite database inside the data directory (created in V1).
DB_FILENAME = "satay.db"

#: Subdirectory holding spilled payload blobs (used from V8).
BLOB_DIR_NAME = "blobs"

#: Schema version this build of satay understands. Migrations are forward-only and
#: keyed on SQLite ``PRAGMA user_version`` (ADR-0017). ``0`` means "no schema yet".
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
