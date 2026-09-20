"""Unit tests for ``satay.control.run_app``'s lifecycle (ADR-0046).

Needs the studio extra (FastAPI/uvicorn); skips cleanly without it, the same posture
``tests/unit/test_devstack.py`` already takes for the orchestrator half of its own file.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("uvicorn")
pytest.importorskip("httpx")

import httpx

import satay
from satay.devstack.lock import DataDirLock, DataDirLockedError
from satay.journal.store import SQLiteStore
from satay.timers import poll_loop_running


@satay.workflow
async def _double(value: int) -> int:
    return value * 2


async def test_yields_a_reachable_base_url_and_token(tmp_path: Path) -> None:
    import satay.control

    async with satay.control.run_app(data_dir=tmp_path / ".satay", interval=0.02) as app:
        assert app.base_url.startswith("http://127.0.0.1:")
        assert app.token
        assert isinstance(app.store, SQLiteStore)

        async with httpx.AsyncClient(base_url=app.base_url) as client:
            authed = await client.get("/runs", headers={"x-satay-token": app.token})
            assert authed.status_code == 200

            unauthed = await client.get("/runs")
            assert unauthed.status_code == 401


async def test_a_second_run_app_on_the_same_dir_is_refused(tmp_path: Path) -> None:
    import satay.control

    data_dir = tmp_path / ".satay"
    async with satay.control.run_app(data_dir=data_dir, interval=0.02):
        with pytest.raises(DataDirLockedError):
            async with satay.control.run_app(data_dir=data_dir, interval=0.02):
                pass  # pragma: no cover - must not be reached

    # After a clean exit the lock is released — a fresh entry succeeds.
    reacquire = DataDirLock(data_dir)
    reacquire.acquire()
    assert reacquire.held
    reacquire.release()


async def test_data_dir_and_store_together_is_a_usage_error(tmp_path: Path) -> None:
    import satay.control

    store = SQLiteStore.open(":memory:")
    try:
        with pytest.raises(TypeError, match="not both"):
            async with satay.control.run_app(store=store, data_dir=tmp_path):
                pass  # pragma: no cover - must not be reached
    finally:
        store.close()


async def test_a_caller_supplied_store_is_left_open_and_unlocked(tmp_path: Path) -> None:
    """``store=`` means the caller owns the lifetime — mirrors satay.run_app's own
    contract (ADR-0030), and skips the data-dir lock since there is no directory to
    lock in the first place (an in-memory store, here)."""
    import satay.control

    store = SQLiteStore.open(":memory:")
    try:
        async with satay.control.run_app(store=store, interval=0.02) as app:
            assert app.store is store
            assert await satay.start(_double, 3, store=store).result() == 6
        assert not poll_loop_running(store)
        assert await store.list_runs()  # still usable: this call did not close it

        # No lock was ever taken, so a second run_app against the same dir elsewhere
        # is unaffected — proven indirectly: DataDirLock only exists per-directory,
        # and store= took none.
    finally:
        store.close()


async def test_teardown_happens_even_when_the_body_raises(tmp_path: Path) -> None:
    import satay.control

    data_dir = tmp_path / ".satay"
    captured: list[SQLiteStore] = []

    with pytest.raises(RuntimeError, match="boom"):
        async with satay.control.run_app(data_dir=data_dir, interval=0.02) as app:
            assert isinstance(app.store, SQLiteStore)
            captured.append(app.store)
            assert poll_loop_running(app.store)
            raise RuntimeError("boom")

    opened = captured[0]
    assert not poll_loop_running(opened)

    # The lock was released on the way out — a fresh entry on the same dir succeeds.
    reacquire = DataDirLock(data_dir)
    reacquire.acquire()
    assert reacquire.held
    reacquire.release()


async def test_server_refuses_a_non_loopback_bind(tmp_path: Path) -> None:
    import satay.control
    from satay.control.security import NonLoopbackBindError

    with pytest.raises(NonLoopbackBindError):
        async with satay.control.run_app(data_dir=tmp_path / ".satay", host="0.0.0.0"):
            pass  # pragma: no cover - must not be reached
