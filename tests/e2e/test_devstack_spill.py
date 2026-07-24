"""End-to-end tests for ``satay dev`` (N20) + payload spill (N19) over real HTTP.

One :class:`DevStack` boots the worker, SQLite store, control/read API, and Studio in a
single process on an ephemeral loopback port; the tests drive it over real HTTP with
``httpx`` (the transport the browser uses). They prove the Q43 session-token smoke path,
that a large task output spills to a blob while the journal keeps a reference and still
rehydrates for Studio, and that redaction runs *after* rehydration on a spilled output.
Requires the ``satay[studio]`` extra; skips cleanly without it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("uvicorn")
pytest.importorskip("httpx")

import httpx

from satay import demo
from satay.config import blob_dir
from satay.control.redaction import REDACTED
from satay.control.security import TOKEN_HEADER
from satay.devstack.lock import DataDirLockedError
from satay.devstack.orchestrator import DevStack


@pytest.fixture(autouse=True)
def _reset() -> None:
    demo.reset_executions()


async def _wait_for_completed(client: object, run_id: str, *, token: str) -> dict[str, object]:
    """Poll the timeline until the worker drives the run terminal (write-then-poll)."""
    headers = {TOKEN_HEADER: token}
    for _ in range(200):
        resp = await client.get(f"/runs/{run_id}/timeline", headers=headers)  # type: ignore[attr-defined]
        if resp.status_code == 200 and resp.json()["status"] in ("completed", "failed"):
            return dict(resp.json())
        await asyncio.sleep(0.02)
    raise AssertionError(f"run {run_id} did not complete in time")


async def test_dev_boots_the_stack_and_the_session_token_guards_the_api(tmp_path: Path) -> None:
    """One dev stack runs worker+store+API+Studio; the guard needs the session token (Q43)."""
    async with DevStack(
        data_dir=tmp_path / ".satay", port=0, worker_interval=0.02, log_level="warning"
    ) as dev:
        assert dev.studio_url() == f"http://127.0.0.1:{dev.port}/?token={dev.token}"
        async with httpx.AsyncClient(base_url=dev.base_url()) as client:
            # No token → rejected; the booted stack's token → accepted (ADR-0014 guard).
            assert (await client.get("/runs")).status_code == 401
            ok = await client.get("/runs", headers={TOKEN_HEADER: dev.token})
            assert ok.status_code == 200
            assert "runs" in ok.json()


async def test_large_output_spills_and_rehydrates_over_http(tmp_path: Path) -> None:
    data_dir = tmp_path / ".satay"
    async with DevStack(
        data_dir=data_dir, port=0, worker_interval=0.02, log_level="warning"
    ) as dev:
        headers = {TOKEN_HEADER: dev.token}
        async with httpx.AsyncClient(base_url=dev.base_url()) as client:
            resp = await client.post(
                "/runs", json={"workflow": "big_output_demo", "input": 7}, headers=headers
            )
            assert resp.status_code == 202
            run_id = resp.json()["run_id"]

            await _wait_for_completed(client, run_id, token=dev.token)

            # The output spilled to a blob on disk, but the read API rehydrates the full
            # value for Studio — spill is invisible end to end.
            detail = await client.get(f"/runs/{run_id}/tasks/big_output_task:0", headers=headers)
            assert detail.status_code == 200
            output = detail.json()["output"]
            assert output["n"] == 7
            assert output["blob"] == "x" * demo.BIG_OUTPUT_SIZE

        assert list(blob_dir(data_dir).glob("*.blob")), "expected a spilled blob on disk"


async def test_sensitive_field_in_a_spilled_output_is_redacted_after_rehydration(
    tmp_path: Path,
) -> None:
    """The redactor runs after blob rehydration, so a secret in a spilled output is scrubbed."""
    async with DevStack(
        data_dir=tmp_path / ".satay", port=0, worker_interval=0.02, log_level="warning"
    ) as dev:
        headers = {TOKEN_HEADER: dev.token}
        async with httpx.AsyncClient(base_url=dev.base_url()) as client:
            resp = await client.post(
                "/runs", json={"workflow": "big_secret_demo", "input": 1}, headers=headers
            )
            run_id = resp.json()["run_id"]
            await _wait_for_completed(client, run_id, token=dev.token)

            detail = await client.get(f"/runs/{run_id}/tasks/big_secret_task:0", headers=headers)
            body = detail.text
            output = detail.json()["output"]
            # Redacted despite living inside a spilled (over-threshold) output.
            assert output["api_key"] == REDACTED
            assert "super-secret-in-a-blob" not in body
            # The non-sensitive part of the spilled payload still rehydrated fully.
            assert output["padding"] == "y" * demo.BIG_OUTPUT_SIZE


async def test_a_second_dev_on_the_same_data_dir_is_refused(tmp_path: Path) -> None:
    data_dir = tmp_path / ".satay"
    async with DevStack(data_dir=data_dir, port=0, worker_interval=0.02, log_level="warning"):
        second = DevStack(data_dir=data_dir, port=0, worker_interval=0.02, log_level="warning")
        with pytest.raises(DataDirLockedError):
            await second.start()
