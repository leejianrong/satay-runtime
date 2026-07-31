"""End-to-end acceptance tests for the version-mismatch policy split (ADR-0023).

``effect_safety`` used to govern three unrelated checks. ADR-0022 took replay divergence
off it; this suite pins the third split — the **code-version mismatch on resume** (N17,
ADR-0010) — and the default it preserves.

Driven through the primary seam (ADR-0011): the public ``satay.start`` API, a temp
``SQLiteStore``, and the ``FaultInjector`` crash hook. ``current_code_version`` is
monkeypatched to model "the process resuming this run is running different code",
exactly as the V7 mismatch test does.
"""

from __future__ import annotations

import logging

import pytest

from satay import demo, versioning
from satay.api.primitives import start
from satay.journal.events import EventType, RunStatus
from satay.journal.store import SQLiteStore
from satay.testing.faults import FaultInjector, SimulatedCrash

_POLICY_ENV_VARS = ("SATAY_EFFECT_SAFETY", "SATAY_NONDETERMINISM", "SATAY_VERSION_MISMATCH")


@pytest.fixture(autouse=True)
def _clean_slate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset the demo counters and unset every policy env var, so the defaults are the
    defaults and not whatever the ambient shell happens to export."""
    demo.reset_executions()
    for var in _POLICY_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


async def _crashed_mid_run(store: SQLiteStore, run_id: str) -> None:
    """Leave ``run_id`` non-terminal (RUNNING) so the next start takes the resume path."""
    injector = FaultInjector()
    injector.crash_after("TaskScheduled")
    with pytest.raises(SimulatedCrash):
        await start(demo.demo, 1, store=store, injector=injector, run_id=run_id).result()
    assert (await store.get_run(run_id)).status is RunStatus.RUNNING


async def test_default_resume_under_changed_code_warns_and_proceeds(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Pins the preserved default (ADR-0023): out of the box a version mismatch on resume
    still warns and lets the resume through, exactly as it did via ``effect_safety=warn``.
    """
    store = SQLiteStore.open(":memory:")
    await _crashed_mid_run(store, "default")
    monkeypatch.setattr(versioning, "current_code_version", lambda: "changed:v2")

    with caplog.at_level(logging.WARNING, logger="satay"):
        result = await start(demo.demo, 1, run_id="default", store=store).result()

    assert result == 4  # the resume proceeded to completion
    assert "mismatch" in caplog.text.lower()
    assert any(e.type is EventType.WORKFLOW_RESUMED for e in await store.read_events("default"))
    store.close()


async def test_effect_safety_off_does_not_disable_version_mismatch_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The defect ADR-0023 fixes: quieting a side-effect warning silently disabled the
    version check. The knobs are independent now — ``effect_safety=off`` leaves a strict
    version policy strict."""
    store = SQLiteStore.open(":memory:")
    await _crashed_mid_run(store, "eso")
    monkeypatch.setattr(versioning, "current_code_version", lambda: "changed:v2")

    resumed = start(
        demo.demo, 1, run_id="eso", store=store, effect_safety="off", version_mismatch="strict"
    )
    with pytest.raises(versioning.VersionMismatchError):
        await resumed.result()

    # Rejected before the resume was recorded: the run is untouched and still resumable.
    events = await store.read_events("eso")
    assert not any(e.type is EventType.WORKFLOW_RESUMED for e in events)
    assert (await store.get_run("eso")).status is RunStatus.RUNNING
    store.close()


async def test_effect_safety_strict_does_not_force_version_mismatch_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The converse leak: ``effect_safety=strict`` must not turn an ``off`` version
    policy into a rejection."""
    store = SQLiteStore.open(":memory:")
    await _crashed_mid_run(store, "essv")
    monkeypatch.setattr(versioning, "current_code_version", lambda: "changed:v2")

    result = await start(
        demo.demo, 1, run_id="essv", store=store, effect_safety="strict", version_mismatch="off"
    ).result()
    assert result == 4
    store.close()


async def test_version_mismatch_off_does_not_silence_the_effect_safety_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The other direction of the split: silencing the version check leaves effect safety
    alone."""
    store = SQLiteStore.open(":memory:")
    with caplog.at_level(logging.WARNING, logger="satay"):
        await start(demo.unguarded_effect_demo, 1, store=store, version_mismatch="off").result()
    assert "effect_safety" in caplog.text
    store.close()


async def test_version_mismatch_off_is_silent_on_a_real_mismatch(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    store = SQLiteStore.open(":memory:")
    await _crashed_mid_run(store, "off")
    monkeypatch.setattr(versioning, "current_code_version", lambda: "changed:v2")

    with caplog.at_level(logging.WARNING, logger="satay"):
        resumed = start(demo.demo, 1, run_id="off", store=store, version_mismatch="off")
        assert await resumed.result() == 4
    assert "mismatch" not in caplog.text.lower()
    store.close()


async def test_the_env_var_moves_the_policy_without_touching_effect_safety(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``SATAY_VERSION_MISMATCH`` is the per-process knob; it is not ``SATAY_EFFECT_SAFETY``."""
    store = SQLiteStore.open(":memory:")
    await _crashed_mid_run(store, "env")
    monkeypatch.setattr(versioning, "current_code_version", lambda: "changed:v2")
    monkeypatch.setenv("SATAY_VERSION_MISMATCH", "strict")

    with pytest.raises(versioning.VersionMismatchError):
        await start(demo.demo, 1, run_id="env", store=store).result()

    # And the reverse: the effect-safety env var alone leaves the version policy at warn.
    monkeypatch.delenv("SATAY_VERSION_MISMATCH")
    monkeypatch.setenv("SATAY_EFFECT_SAFETY", "strict")
    assert await start(demo.demo, 1, run_id="env", store=store).result() == 4
    store.close()
