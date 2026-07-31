"""End-to-end: ``satay dev --app`` runs *your* workflows (KAN-448) and honours the
project policies (KAN-458).

The capability under test did not exist before: a standalone dev stack imported none of
the user's code, so its registry was empty, its poll loop could not resolve a workflow by
name in order to wake a parked run, and ``POST /runs`` could not start anything. Here a
throwaway user module is written to disk, loaded the way ``--app`` loads it, and then a
real :class:`DevStack` — same store, same HTTP surface a browser would use — is asked to
wake a run that was parked by a *different* store instance, and to start a fresh one.

Requires the ``satay[studio]`` extra; skips cleanly without it.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("uvicorn")
pytest.importorskip("httpx")

import httpx

import satay
from satay.api.registry import REGISTRY
from satay.config import (
    EffectSafety,
    NondeterminismPolicy,
    VersionMismatchPolicy,
    db_path,
)
from satay.control.security import TOKEN_HEADER
from satay.devstack.appload import load_app
from satay.devstack.orchestrator import DevStack, run_dev
from satay.journal.store import SQLiteStore

MODULE_NAME = "kan448_e2e_app"

_APP_SOURCE = '''
"""A stand-in for the user's own project module."""

import satay


@satay.task()
async def kan448_render(name: str) -> str:
    return f"report for {name}"


@satay.task(retries=1, side_effect=True)
async def kan448_charge(name: str) -> str:
    return f"charged {name}"


@satay.workflow
async def kan448_nightly(name: str) -> str:
    await satay.sleep(0.2)
    return await kan448_render(name)


@satay.workflow
async def kan448_unguarded(name: str) -> str:
    return await kan448_charge(name)
'''


@pytest.fixture(scope="module")
def app_module(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Write a user module to a throwaway project and load it the way ``--app`` does."""
    project = tmp_path_factory.mktemp("kan448_project")
    (project / f"{MODULE_NAME}.py").write_text(_APP_SOURCE, encoding="utf-8")
    modules_before = set(sys.modules)
    path_before = list(sys.path)

    report = load_app([MODULE_NAME], project_dir=project)
    assert "kan448_nightly" in report.workflows

    yield MODULE_NAME

    for name in set(sys.modules) - modules_before:
        del sys.modules[name]
    sys.path[:] = path_before


async def _poll_status(client: httpx.AsyncClient, run_id: str, *, token: str) -> str:
    """Poll the read API until the run reaches a terminal status."""
    headers = {TOKEN_HEADER: token}
    for _ in range(300):
        resp = await client.get(f"/runs/{run_id}/timeline", headers=headers)
        if resp.status_code == 200 and resp.json()["status"] in ("completed", "failed"):
            return str(resp.json()["status"])
        await asyncio.sleep(0.02)
    raise AssertionError(f"run {run_id} did not settle in time")


async def _park_a_run(data_dir: Path, name: str) -> str:
    """Start a run in a *separate* store (a user process), leaving it parked on a timer."""
    data_dir.mkdir(parents=True, exist_ok=True)
    store = SQLiteStore.open(db_path(data_dir))
    try:
        workflow = REGISTRY.get_workflow("kan448_nightly")
        assert workflow is not None
        handle = satay.start(workflow.fn, name, store=store)
        await handle.result()
        assert await handle.status() == "waiting"
        return handle.run_id
    finally:
        store.close()


# -- the capability KAN-448 is about ---------------------------------------------


async def test_dev_stack_wakes_a_run_parked_by_another_process(
    app_module: str, tmp_path: Path
) -> None:
    """The headline: a run parked elsewhere is woken and completed by ``satay dev --app``."""
    data_dir = tmp_path / ".satay"
    run_id = await _park_a_run(data_dir, "acme")

    async with (
        DevStack(data_dir=data_dir, port=0, worker_interval=0.02, log_level="warning") as dev,
        httpx.AsyncClient(base_url=dev.base_url()) as client,
    ):
        assert await _poll_status(client, run_id, token=dev.token) == "completed"


async def test_control_api_starts_a_workflow_from_a_loaded_module(
    app_module: str, tmp_path: Path
) -> None:
    """``POST /runs`` can start a user workflow, and the same stack drives it to the end."""
    async with DevStack(
        data_dir=tmp_path / ".satay", port=0, worker_interval=0.02, log_level="warning"
    ) as dev:
        headers = {TOKEN_HEADER: dev.token}
        async with httpx.AsyncClient(base_url=dev.base_url()) as client:
            resp = await client.post(
                "/runs", json={"workflow": "kan448_nightly", "input": "beta"}, headers=headers
            )
            assert resp.status_code == 202
            run_id = resp.json()["run_id"]

            # It parks on the durable sleep, and this stack's own poll loop wakes it.
            assert await _poll_status(client, run_id, token=dev.token) == "completed"


def test_a_bad_app_module_aborts_the_boot_instead_of_running_empty(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Silence was the bug: an unimportable ``--app`` must exit non-zero, naming it."""
    code = run_dev(
        data_dir=str(tmp_path / ".satay"),
        port=0,
        app_modules=["kan448_definitely_not_installed"],
        project_dir=str(tmp_path),
    )

    assert code == 2
    err = capsys.readouterr().err
    assert "kan448_definitely_not_installed" in err
    # It failed before touching the data directory — no lock, no half-booted stack.
    assert not (tmp_path / ".satay").exists()


# -- KAN-458: the dev stack must honour the project policies ----------------------


def test_dev_stack_resolves_every_policy_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``SATAY_NONDETERMINISM=warn satay dev`` must mean what it says (KAN-458).

    All three project policies, not two: a policy the dev stack forgets to thread is
    silently ignored, which is the same bug in a different place (ADR-0022/0023).
    """
    monkeypatch.setenv("SATAY_NONDETERMINISM", "warn")
    monkeypatch.setenv("SATAY_EFFECT_SAFETY", "strict")
    monkeypatch.setenv("SATAY_VERSION_MISMATCH", "strict")

    stack = DevStack(data_dir=tmp_path / ".satay", port=0, log_level="warning")

    assert stack.nondeterminism is NondeterminismPolicy.WARN
    assert stack.effect_safety is EffectSafety.STRICT
    assert stack.version_mismatch is VersionMismatchPolicy.STRICT


def test_dev_stack_defaults_match_the_documented_defaults(tmp_path: Path) -> None:
    stack = DevStack(data_dir=tmp_path / ".satay", port=0, log_level="warning")

    assert stack.effect_safety is EffectSafety.WARN
    assert stack.nondeterminism is NondeterminismPolicy.STRICT
    assert stack.version_mismatch is VersionMismatchPolicy.WARN


def test_the_worker_receives_every_policy_the_stack_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pass-through itself: whatever the stack resolved is what the worker gets.

    Guards the regression directly — before KAN-458 the stack built its worker with no
    policy arguments at all, so the worker fell back to its own defaults. Captures the
    keyword arguments at the constructor seam rather than reading the worker's internals.
    """
    monkeypatch.setenv("SATAY_EFFECT_SAFETY", "off")
    monkeypatch.setenv("SATAY_NONDETERMINISM", "off")
    monkeypatch.setenv("SATAY_VERSION_MISMATCH", "strict")
    seen: dict[str, object] = {}

    import satay.devstack.orchestrator as orchestrator

    real_worker = orchestrator.TimerEventWorker

    def _spy(**kwargs: object) -> object:
        seen.update(kwargs)
        return real_worker(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(orchestrator, "TimerEventWorker", _spy)

    async def _boot() -> None:
        async with DevStack(
            data_dir=tmp_path / ".satay", port=0, worker_interval=0.02, log_level="warning"
        ):
            pass

    asyncio.run(_boot())

    assert seen["effect_safety"] is EffectSafety.OFF
    assert seen["nondeterminism"] is NondeterminismPolicy.OFF
    assert seen["version_mismatch"] is VersionMismatchPolicy.STRICT


async def test_effect_safety_off_from_the_environment_reaches_the_worker(
    app_module: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Observable pass-through: ``off`` silences the warning the default ``warn`` emits.

    ``kan448_unguarded`` calls a retryable side-effecting task that declares no
    idempotency strategy, which logs under ``effect_safety=warn``. Before KAN-458 the
    dev stack built its worker without either policy, so the environment was ignored and
    the warning was logged regardless.
    """
    monkeypatch.setenv("SATAY_EFFECT_SAFETY", "off")

    with caplog.at_level(logging.WARNING, logger="satay.replay.engine"):
        async with DevStack(
            data_dir=tmp_path / ".satay", port=0, worker_interval=0.02, log_level="warning"
        ) as dev:
            assert dev.effect_safety is EffectSafety.OFF
            headers = {TOKEN_HEADER: dev.token}
            async with httpx.AsyncClient(base_url=dev.base_url()) as client:
                resp = await client.post(
                    "/runs",
                    json={"workflow": "kan448_unguarded", "input": "acme"},
                    headers=headers,
                )
                run_id = resp.json()["run_id"]
                assert await _poll_status(client, run_id, token=dev.token) == "completed"

    assert not [r for r in caplog.records if "effect_safety" in r.getMessage()]


async def test_effect_safety_warn_still_warns_by_default(
    app_module: str, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The control for the test above: the default really does log this condition."""
    with caplog.at_level(logging.WARNING, logger="satay.replay.engine"):
        async with DevStack(
            data_dir=tmp_path / ".satay", port=0, worker_interval=0.02, log_level="warning"
        ) as dev:
            headers = {TOKEN_HEADER: dev.token}
            async with httpx.AsyncClient(base_url=dev.base_url()) as client:
                resp = await client.post(
                    "/runs",
                    json={"workflow": "kan448_unguarded", "input": "acme"},
                    headers=headers,
                )
                run_id = resp.json()["run_id"]
                assert await _poll_status(client, run_id, token=dev.token) == "completed"

    assert [r for r in caplog.records if "effect_safety" in r.getMessage()]
