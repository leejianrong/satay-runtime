"""Exclusive data-directory lock for ``satay dev`` (ADR-0017/Q54).

The durability model rests on a **single writer** (ADR-0012), but SQLite's WAL does not
stop a second process opening the same database, so two ``satay dev`` instances on one
``./.satay/`` would race the journal into corruption — the worst failure for a
durability tool. ``satay dev`` therefore takes an **exclusive OS advisory lock** on a
lockfile in the data directory at startup, refuses to start on contention with an error
**naming the holding process**, and releases the lock on clean shutdown.

Stdlib only (``fcntl`` advisory locks), local-disk only, consistent with the platform
scope (ADR-0019). Kept out of the studio import chain so it drags in nothing heavy.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import TracebackType

try:  # fcntl is POSIX-only; the platform scope is local Unix/WSL (ADR-0019).
    import fcntl

    _HAVE_FCNTL = True
except ImportError:  # pragma: no cover - non-POSIX platforms are out of scope
    _HAVE_FCNTL = False

#: Name of the lockfile inside the data directory.
LOCKFILE_NAME = "dev.lock"


class DataDirLockedError(RuntimeError):
    """Raised when another ``satay dev`` already holds the lock on a data directory."""

    def __init__(self, path: Path, holder: str) -> None:
        self.path = path
        self.holder = holder
        super().__init__(
            f"another satay dev process holds the lock on {path} ({holder}); refusing to "
            f"start a second writer on the same data directory, which would race the "
            f"single-writer journal into corruption (ADR-0017/Q54). Stop that process, or "
            f"run with a different --data-dir."
        )


class DataDirLock:
    """An exclusive advisory lock on a data directory, released on close.

    Use as a context manager or call :meth:`acquire` / :meth:`release`. On contention
    :meth:`acquire` raises :class:`DataDirLockedError` naming the holding process.
    """

    def __init__(self, data_dir: Path | str) -> None:
        self._data_dir = Path(data_dir)
        self._path = self._data_dir / LOCKFILE_NAME
        self._fd: int | None = None

    @property
    def path(self) -> Path:
        """The lockfile path."""
        return self._path

    @property
    def held(self) -> bool:
        """Whether this lock is currently held by us."""
        return self._fd is not None

    def acquire(self) -> None:
        """Acquire the exclusive lock, or raise :class:`DataDirLockedError` if held."""
        self._data_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            if _HAVE_FCNTL:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    holder = _read_holder(fd)
                    os.close(fd)
                    raise DataDirLockedError(self._path, holder) from exc
            os.ftruncate(fd, 0)
            os.write(fd, _holder_line().encode("utf-8"))
            os.fsync(fd)
        except DataDirLockedError:
            raise
        except OSError:  # pragma: no cover - defensive: unexpected fd failure
            os.close(fd)
            raise
        self._fd = fd

    def release(self) -> None:
        """Release the lock (a no-op if not held). The lockfile itself is left on disk."""
        if self._fd is None:
            return
        try:
            if _HAVE_FCNTL:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> DataDirLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()


def _holder_line() -> str:
    """A short, human-readable description of the current holder written to the lockfile."""
    return f"pid={os.getpid()}"


def _read_holder(fd: int) -> str:
    """Read the holder description a contending process wrote, for the error message."""
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        data = os.read(fd, 256).decode("utf-8", errors="replace").strip()
    except OSError:  # pragma: no cover - defensive
        return "unknown process"
    return data or "unknown process"


__all__ = ["LOCKFILE_NAME", "DataDirLock", "DataDirLockedError"]
