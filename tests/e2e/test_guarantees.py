"""E2E: nondeterminism detection, effect-safety policy, and usage self-report.

Driven through the public seam (ADR-0011). The two policies are **separate** knobs with
different defaults (ADR-0022): ``nondeterminism`` defaults to ``strict`` so a divergent
replay raises, while ``effect_safety`` keeps its ``warn`` dev default. Both are asserted
through observable outcomes — the raised public errors, the returned result, and the
``satay`` logger.
"""

from __future__ import annotations

import logging

import pytest

import satay
from satay import EffectSafetyError, NondeterminismError, demo
from satay.api.primitives import start
from satay.control.api import ControlAPI
from satay.control.commands import CommandQueue
from satay.journal.events import EventType
from satay.journal.store import SQLiteStore
from satay.journal.timeline import model_usage
from satay.testing.faults import FaultInjector, SimulatedCrash
from satay.timers import TimerEventWorker

#: ``reorder_original(1)`` = ``nd_second(nd_first(1))`` = ``(1 + 1) * 2``.
CORRECT_RESULT = 4
#: What the reordered body returns when the divergence is allowed through: ``nd_second(1)``
#: executes fresh (``1 * 2``) and ``nd_first`` is answered by its recorded result. A
#: plausible, wrong number — the reason ``strict`` is the default.
WRONG_RESULT = 2


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch) -> None:
    demo.reset_executions()
    # These tests assert policy *defaults*, so an ambient env var must not leak in.
    monkeypatch.delenv("SATAY_NONDETERMINISM", raising=False)
    monkeypatch.delenv("SATAY_EFFECT_SAFETY", raising=False)


async def _crash_after_first_completion(store: SQLiteStore) -> str:
    """Drive ``reorder_original`` and crash after ``nd_first`` completes; return run_id."""
    injector = FaultInjector()
    injector.crash_after("TaskCompleted")
    handle = start(demo.reorder_original, 1, store=store, injector=injector)
    with pytest.raises(SimulatedCrash):
        await handle.result()
    return handle.run_id


async def test_uninterrupted_run_returns_the_correct_result() -> None:
    """Baseline: the un-reordered body is what ``CORRECT_RESULT`` means."""
    store = SQLiteStore.open(":memory:")
    assert await start(demo.reorder_original, 1, store=store).result() == CORRECT_RESULT
    store.close()


async def test_divergent_replay_raises_by_default() -> None:
    """The headline of ADR-0022: no policy argument, no env var — divergence raises.

    Before the split this returned ``WRONG_RESULT`` and reported success.
    """
    store = SQLiteStore.open(":memory:")
    run_id = await _crash_after_first_completion(store)

    resumed = start(demo.reorder_edited, 1, run_id=run_id, store=store)
    with pytest.raises(NondeterminismError):
        await resumed.result()
    store.close()


async def test_divergent_replay_raises_nondeterminism_error_under_strict() -> None:
    store = SQLiteStore.open(":memory:")
    run_id = await _crash_after_first_completion(store)

    # Resume with the reordered body: position 0 issued nd_second, journal has nd_first.
    resumed = start(demo.reorder_edited, 1, run_id=run_id, store=store, nondeterminism="strict")
    with pytest.raises(NondeterminismError) as excinfo:
        await resumed.result()
    assert excinfo.value.position == 0
    assert excinfo.value.expected == "nd_first"
    assert excinfo.value.actual == "nd_second"
    store.close()


async def test_divergent_replay_warns_and_returns_a_wrong_result_under_opt_in_warn(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``warn`` is an explicit opt-in now, and this is exactly what it buys you."""
    store = SQLiteStore.open(":memory:")
    run_id = await _crash_after_first_completion(store)

    resumed = start(demo.reorder_edited, 1, run_id=run_id, store=store, nondeterminism="warn")
    with caplog.at_level(logging.WARNING, logger="satay"):
        result = await resumed.result()  # warns and continues — no NondeterminismError
    assert "nondeterministic" in caplog.text
    assert result == WRONG_RESULT != CORRECT_RESULT
    store.close()


async def test_divergent_replay_is_silent_under_opt_in_off(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = SQLiteStore.open(":memory:")
    run_id = await _crash_after_first_completion(store)

    resumed = start(demo.reorder_edited, 1, run_id=run_id, store=store, nondeterminism="off")
    with caplog.at_level(logging.WARNING, logger="satay"):
        result = await resumed.result()
    assert "nondeterministic" not in caplog.text
    assert result == WRONG_RESULT
    store.close()


async def test_the_env_var_opts_a_whole_process_into_warn(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("SATAY_NONDETERMINISM", "warn")
    store = SQLiteStore.open(":memory:")
    run_id = await _crash_after_first_completion(store)

    resumed = start(demo.reorder_edited, 1, run_id=run_id, store=store)
    with caplog.at_level(logging.WARNING, logger="satay"):
        assert await resumed.result() == WRONG_RESULT
    assert "nondeterministic" in caplog.text
    store.close()


async def test_effect_safety_off_does_not_disable_the_nondeterminism_check() -> None:
    """The knobs are independent: silencing effect safety must not silence divergence."""
    store = SQLiteStore.open(":memory:")
    run_id = await _crash_after_first_completion(store)

    resumed = start(demo.reorder_edited, 1, run_id=run_id, store=store, effect_safety="off")
    with pytest.raises(NondeterminismError):
        await resumed.result()
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


async def test_nondeterminism_off_does_not_silence_the_effect_safety_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The other direction of the split: silencing divergence leaves effect safety alone."""
    store = SQLiteStore.open(":memory:")
    with caplog.at_level(logging.WARNING, logger="satay"):
        await start(demo.unguarded_effect_demo, 1, store=store, nondeterminism="off").result()
    assert "effect_safety" in caplog.text
    store.close()


# -- child runs inherit the parent's policy (ADR-0022) ---------------------------------

#: Flipped by a test to stand in for editing ``ndchild_body``'s source between two runs.
#: Reading it inside a workflow body is exactly the nondeterminism being provoked.
_CHILD_BODY = {"edited": False}


@satay.task()
async def ndchild_first(value: int) -> int:
    return value + 1


@satay.task()
async def ndchild_second(value: int) -> int:
    return value * 2


@satay.workflow
async def ndchild_body(value: int) -> int:
    """A two-call child whose call order flips once ``_CHILD_BODY['edited']`` is set."""
    if _CHILD_BODY["edited"]:
        return await ndchild_first(await ndchild_second(value))
    return await ndchild_second(await ndchild_first(value))


@satay.workflow
async def ndchild_parent(value: int) -> int:
    """Start the child and return its result, so a child divergence surfaces here."""
    handle = await satay.start_child(ndchild_body, value)
    result: int = await handle.result()
    return result


async def _crash_inside_the_child(store: SQLiteStore) -> str:
    """Drive the parent, crashing after the child's first task completes; return parent id."""
    injector = FaultInjector()
    injector.crash_after("TaskCompleted")
    handle = start(ndchild_parent, 1, store=store, injector=injector)
    with pytest.raises(SimulatedCrash):
        await handle.result()
    return handle.run_id


async def test_a_child_run_diverging_raises_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    store = SQLiteStore.open(":memory:")
    run_id = await _crash_inside_the_child(store)

    monkeypatch.setitem(_CHILD_BODY, "edited", True)
    with pytest.raises(NondeterminismError):
        await start(ndchild_parent, 1, run_id=run_id, store=store).result()
    store.close()


async def test_a_child_run_inherits_the_parents_opt_in_to_warn(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A child engine must not fall back to the strict default when its parent opted out."""
    store = SQLiteStore.open(":memory:")
    run_id = await _crash_inside_the_child(store)

    monkeypatch.setitem(_CHILD_BODY, "edited", True)
    resumed = start(ndchild_parent, 1, run_id=run_id, store=store, nondeterminism="warn")
    with caplog.at_level(logging.WARNING, logger="satay"):
        result = await resumed.result()  # inherited warn — no NondeterminismError
    assert "nondeterministic" in caplog.text
    assert result == WRONG_RESULT
    store.close()


async def test_the_worker_and_fork_paths_default_to_strict_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not every engine is built by ``satay.start``: the worker and fork paths default too.

    Nothing here passes a policy — the worker, the command dispatch, and the fork's engine
    each fall back to their own default, and all three must be ``strict``.
    """
    store = SQLiteStore.open(":memory:")
    queue = CommandQueue()
    control = ControlAPI(store, queue)
    worker = TimerEventWorker(store=store, commands=queue)

    await start(ndchild_body, 1, store=store, run_id="src").result()
    events = await store.read_events("src")
    fork_point = min(e.seq for e in events if e.type is EventType.TASK_COMPLETED)

    monkeypatch.setitem(_CHILD_BODY, "edited", True)
    await control.fork("src", fork_point)
    with pytest.raises(NondeterminismError):
        await worker.tick()  # the worker drains the fork command and drives the new run
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
