"""Unit tests for the dev-stack lock and orchestrator lifecycle (N20, ADR-0017/Q54).

The orchestrator needs the studio extra (FastAPI/uvicorn), so those tests skip cleanly
without it; the lockfile tests are pure stdlib and always run.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from satay.devstack.lock import DataDirLock, DataDirLockedError


def test_second_lock_on_the_same_dir_is_refused_naming_the_holder(tmp_path: Path) -> None:
    """A second `satay dev` on one ./.satay/ is refused, protecting the single writer."""
    data_dir = tmp_path / ".satay"
    first = DataDirLock(data_dir)
    first.acquire()
    try:
        assert first.held
        second = DataDirLock(data_dir)
        with pytest.raises(DataDirLockedError) as excinfo:
            second.acquire()
        # The error names the holding process (its pid) and the guarded dir.
        assert f"pid={os.getpid()}" in str(excinfo.value)
        assert str(first.path) in str(excinfo.value)
        assert not second.held
    finally:
        first.release()


def test_lock_releases_and_can_be_reacquired(tmp_path: Path) -> None:
    data_dir = tmp_path / ".satay"
    lock = DataDirLock(data_dir)
    lock.acquire()
    lock.release()
    assert not lock.held
    # After release, a fresh acquire (a later `satay dev`) succeeds.
    again = DataDirLock(data_dir)
    again.acquire()
    assert again.held
    again.release()


def test_lock_context_manager(tmp_path: Path) -> None:
    data_dir = tmp_path / ".satay"
    with DataDirLock(data_dir) as lock:
        assert lock.held
        assert lock.path.exists()
    assert not lock.held


# -- orchestrator (studio extra) -------------------------------------------------

pytest.importorskip("fastapi")
pytest.importorskip("uvicorn")


async def test_orchestrator_starts_and_stops_parts_in_clean_order(tmp_path: Path) -> None:
    from satay.devstack.orchestrator import DevStack

    stack = DevStack(
        data_dir=tmp_path / ".satay", port=0, worker_interval=0.02, log_level="warning"
    )
    await stack.start()
    try:
        # Startup order: lock, then store, then worker, then the HTTP server.
        assert stack.started_parts == ["lock", "store", "worker", "server"]
        assert stack.port != 0  # an ephemeral port was actually bound
        assert stack.token
        assert stack.studio_url().startswith(f"http://127.0.0.1:{stack.port}/?token=")
    finally:
        await stack.stop()

    # After a clean shutdown the lock is released — a second dev stack can take the dir.
    reacquire = DataDirLock(tmp_path / ".satay")
    reacquire.acquire()
    assert reacquire.held
    reacquire.release()


async def test_second_devstack_on_the_same_dir_is_refused(tmp_path: Path) -> None:
    from satay.devstack.orchestrator import DevStack

    data_dir = tmp_path / ".satay"
    first = DevStack(data_dir=data_dir, port=0, worker_interval=0.02, log_level="warning")
    await first.start()
    try:
        second = DevStack(data_dir=data_dir, port=0, worker_interval=0.02, log_level="warning")
        with pytest.raises(DataDirLockedError):
            await second.start()
    finally:
        await first.stop()
