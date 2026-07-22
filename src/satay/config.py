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
from pathlib import Path

#: Name of the project-local data directory (ADR-0017).
DEFAULT_DATA_DIR_NAME = ".satay"

#: Environment variable that overrides the data directory.
DATA_DIR_ENV_VAR = "SATAY_DATA_DIR"

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
