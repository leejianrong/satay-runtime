"""E2E: nondeterminism detection, effect-safety policy, and usage self-report.

Driven through the public seam (ADR-0011). The safety-mode split (dev warns / strict
fails) is asserted by capturing the ``satay`` logger and by the raised public errors.
"""

from __future__ import annotations

import logging

import pytest

from satay import EffectSafetyError, NondeterminismError, demo
from satay.api.primitives import start
from satay.journal.store import SQLiteStore
from satay.journal.timeline import model_usage
from satay.testing.faults import FaultInjector, SimulatedCrash


@pytest.fixture(autouse=True)
def _reset() -> None:
    demo.reset_executions()


async def _crash_after_first_completion(store: SQLiteStore) -> str:
    """Drive ``reorder_original`` and crash after ``nd_first`` completes; return run_id."""
    injector = FaultInjector()
    injector.crash_after("TaskCompleted")
    handle = start(demo.reorder_original, 1, store=store, injector=injector)
    with pytest.raises(SimulatedCrash):
        await handle.result()
    return handle.run_id


async def test_divergent_replay_raises_nondeterminism_error_under_strict() -> None:
    store = SQLiteStore.open(":memory:")
    run_id = await _crash_after_first_completion(store)

    # Resume with the reordered body: position 0 issued nd_second, journal has nd_first.
    resumed = start(demo.reorder_edited, 1, run_id=run_id, store=store, effect_safety="strict")
    with pytest.raises(NondeterminismError) as excinfo:
        await resumed.result()
    assert excinfo.value.position == 0
    assert excinfo.value.expected == "nd_first"
    assert excinfo.value.actual == "nd_second"
    store.close()


async def test_divergent_replay_warns_but_does_not_fail_in_dev(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = SQLiteStore.open(":memory:")
    run_id = await _crash_after_first_completion(store)

    resumed = start(demo.reorder_edited, 1, run_id=run_id, store=store, effect_safety="warn")
    with caplog.at_level(logging.WARNING, logger="satay"):
        await resumed.result()  # warns and continues — no NondeterminismError
    assert "nondeterministic" in caplog.text
    store.close()


async def test_strict_rejects_unguarded_retryable_side_effect() -> None:
    store = SQLiteStore.open(":memory:")
    handle = start(demo.unguarded_effect_demo, 1, store=store, effect_safety="strict")
    with pytest.raises(EffectSafetyError) as excinfo:
        await handle.result()
    assert excinfo.value.task_name == "unguarded_effect"
    # Rejected at schedule time: the side-effecting body never ran.
    assert demo.execution_count("unguarded_effect") == 0
    store.close()


async def test_warn_emits_a_satay_logger_warning_for_unguarded_side_effect(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = SQLiteStore.open(":memory:")
    with caplog.at_level(logging.WARNING, logger="satay"):
        await start(demo.unguarded_effect_demo, 1, store=store, effect_safety="warn").result()
    assert "effect_safety" in caplog.text
    assert demo.execution_count("unguarded_effect") == 1  # warn logs but runs
    store.close()


async def test_off_emits_no_effect_safety_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = SQLiteStore.open(":memory:")
    with caplog.at_level(logging.WARNING, logger="satay"):
        await start(demo.unguarded_effect_demo, 1, store=store, effect_safety="off").result()
    assert "effect_safety" not in caplog.text
    store.close()


async def test_record_model_usage_reaches_the_usage_slot() -> None:
    store = SQLiteStore.open(":memory:")
    await start(demo.usage_demo, 1, store=store).result()
    events = await store.read_events((await store.list_runs())[0])
    assert model_usage(events) == [{"model": "demo-model", "input_tokens": 10, "output_tokens": 5}]
    store.close()


async def test_non_reporting_task_records_no_usage() -> None:
    store = SQLiteStore.open(":memory:")
    await start(demo.quiet_demo, 1, store=store).result()
    events = await store.read_events((await store.list_runs())[0])
    assert model_usage(events) == []
    store.close()
