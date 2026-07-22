"""E2E: idempotency keys, keyed start, and at-least-once with a key-guarded effect.

Driven through the public seam (ADR-0011). "Reused vs re-executed" and "effect ran
once vs twice" are proven by the demo execution-count marker and the journal.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from satay import demo, task_context
from satay.api.decorators import task, workflow
from satay.api.primitives import start
from satay.journal.events import EventType
from satay.journal.store import SQLiteStore
from satay.testing.clock import ManualClock
from satay.testing.faults import FaultInjector, SimulatedCrash
from satay.testing.rng import SeededRng

_KEYS: list[str] = []


@task(retries=2)
async def key_retry(value: int) -> int:
    _KEYS.append(task_context().idempotency_key)
    if len(_KEYS) < 3:
        raise RuntimeError("retry to observe key stability")
    return value


@workflow
async def key_retry_wf(value: int) -> int:
    return await key_retry(value)


@task()
async def key_echo(value: int) -> str:
    return task_context().idempotency_key


@workflow
async def key_two_wf(value: int) -> list[str]:
    first = await key_echo(value)
    second = await key_echo(value)
    return [first, second]


@pytest.fixture(autouse=True)
def _reset() -> None:
    demo.reset_executions()
    _KEYS.clear()


async def test_key_is_stable_across_retries_and_readable_via_ctx(
    drain: Callable[..., Awaitable[Any]],
) -> None:
    clock = ManualClock()
    store = SQLiteStore.open(":memory:")
    handle = start(key_retry_wf, 42, store=store, clock=clock, rng=SeededRng(1234))
    result = await drain(lambda: handle.result(), clock)

    assert result == 42
    assert len(_KEYS) == 3  # three physical attempts each read ctx.idempotency_key
    assert len(set(_KEYS)) == 1  # ... and saw the *same* key every time
    assert len(_KEYS[0]) == 64  # a sha256 hex digest
    store.close()


async def test_key_is_distinct_across_invocations() -> None:
    store = SQLiteStore.open(":memory:")
    result = await start(key_two_wf, 1, store=store).result()
    assert result[0] != result[1]  # two durable calls → two distinct keys
    store.close()


async def test_keyed_start_returns_the_same_run_and_does_no_duplicate_work() -> None:
    store = SQLiteStore.open(":memory:")
    first = start(demo.demo, 3, store=store, idempotency_key="order-1")
    r1 = await first.result()
    assert r1 == 8  # (3+1)=4, (4*2)=8
    counts = (demo.execution_count("step_one"), demo.execution_count("step_two"))

    # A repeated key resolves to the same logical run — no new run, no re-execution.
    again = start(demo.demo, 3, store=store, idempotency_key="order-1")
    r2 = await again.result()
    assert r2 == r1
    assert again.run_id == first.run_id
    assert (demo.execution_count("step_one"), demo.execution_count("step_two")) == counts
    assert len(await store.list_runs()) == 1
    store.close()


async def test_ambiguous_completion_reruns_but_key_guarded_effect_runs_once() -> None:
    store = SQLiteStore.open(":memory:")
    injector = FaultInjector()
    # Crash right after the first attempt records its failure — after the side effect
    # ran but before the task completed (an ambiguous, in-flight logical task).
    injector.crash_after("TaskAttemptFailed")

    handle = start(demo.interrupted_effect_demo, 7, store=store, injector=injector)
    with pytest.raises(SimulatedCrash):
        await handle.result()
    assert demo.execution_count("interrupted_effect_applied") == 1  # effect ran once
    assert demo.execution_count("interrupted_effect_body") == 1  # attempt 1 body

    # Restart: the ambiguous task re-runs (at-least-once), but the idempotency-key
    # guard makes the external effect run exactly once across the re-execution.
    resumed = start(demo.interrupted_effect_demo, 7, run_id=handle.run_id, store=store)
    result = await resumed.result()

    assert result == 8
    assert demo.execution_count("interrupted_effect_body") == 2  # re-ran the body
    assert demo.execution_count("interrupted_effect_applied") == 1  # ... effect STILL once
    events = await store.read_events(handle.run_id)
    assert events[-1].type is EventType.WORKFLOW_COMPLETED

    # A cleanly completed run is a no-op on a further start (not re-run).
    noop = start(demo.interrupted_effect_demo, 7, run_id=handle.run_id, store=store)
    assert await noop.result() == 8
    assert demo.execution_count("interrupted_effect_body") == 2  # unchanged
    store.close()
