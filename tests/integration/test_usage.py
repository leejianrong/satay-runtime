"""Integration test: model-usage self-report persists and is retrievable (N14, ADR-0008).

Two guarantees live here. The V2 one: what a task self-reports through
``ctx.record_model_usage`` survives to the journal and the read path finds it. And the
KAN-479 one: an attempt that **failed** was billed just the same, so its usage is flushed
onto ``TaskAttemptFailed`` and counted by default — a run that never completes must not
report itself as free.

The spend assertions compare the journal against ``BILLED``, an out-of-band meter the fake
model appends to on every physical call. That is the only honest reference: the provider
charges per call made, not per call whose answer parsed.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterable
from typing import Any

import pytest

from satay import WorkflowFailedError, demo
from satay.api.context import task_context
from satay.api.decorators import task, workflow
from satay.api.primitives import start
from satay.control.views import task_detail
from satay.journal.events import Event, EventType
from satay.journal.store import SQLiteStore
from satay.journal.timeline import model_usage
from satay.testing.faults import SimulatedCrash

#: Input tokens the fake bills for attempt *n*. Rising, so a total pins down exactly which
#: attempts were counted rather than merely how many.
TOKENS_PER_ATTEMPT = 1_000

#: Every physical model call, billed whether or not its answer was usable — the meter the
#: journal is checked against (``examples/agentic_dag_demo.py`` keeps the same one).
BILLED: list[int] = []


@pytest.fixture(autouse=True)
def _reset() -> None:
    demo.reset_executions()
    BILLED.clear()


class MalformedAnswerError(ValueError):
    """The provider answered, billed us, and the answer did not parse."""


@task(retries=2)
async def flaky_model_call(succeed_on_attempt: int) -> str:
    """Bill on every attempt; return something usable only on ``succeed_on_attempt``."""
    ctx = task_context()
    input_tokens = TOKENS_PER_ATTEMPT * ctx.attempt
    BILLED.append(input_tokens)
    ctx.record_model_usage(
        model="fake-1", input_tokens=input_tokens, output_tokens=1, attempt=ctx.attempt
    )
    if ctx.attempt < succeed_on_attempt:
        raise MalformedAnswerError(f"attempt {ctx.attempt} billed {input_tokens} tokens of garbage")
    return "parsed"


@workflow
async def priced_call(succeed_on_attempt: int) -> str:
    """One logical model call, retried under the task's own policy."""
    return await flaky_model_call(succeed_on_attempt)


@task()
async def abandoned_model_call() -> str:
    """Bills, then the worker dies before the runtime can journal anything."""
    task_context().record_model_usage(model="fake-1", input_tokens=9_000, output_tokens=1)
    raise SimulatedCrash("worker died mid-attempt")


@workflow
async def abandoned_call(_unused: int) -> str:
    return await abandoned_model_call()


def input_tokens(entries: Iterable[dict[str, Any]]) -> int:
    """Total input tokens across usage entries — "what does this run say it cost"."""
    return sum(int(entry.get("input_tokens", 0)) for entry in entries)


def payload_usage(events: Iterable[Event], event_type: EventType) -> list[dict[str, Any]]:
    """The usage entries carried by every event of ``event_type``."""
    return [
        entry
        for event in events
        if event.type is event_type
        for entry in event.payload.get("usage", [])
    ]


async def _drive(succeed_on_attempt: int) -> tuple[SQLiteStore, str, list[Event]]:
    """Run ``priced_call`` to a terminal state and hand back its journal."""
    store = SQLiteStore.open(":memory:")
    handle = start(priced_call, succeed_on_attempt, store=store)
    with contextlib.suppress(WorkflowFailedError):
        await handle.result()  # a run that exhausts its retries is one of the cases here
    return store, handle.run_id, list(await store.read_events(handle.run_id))


async def test_record_model_usage_persists_and_the_read_path_retrieves_it() -> None:
    store = SQLiteStore.open(":memory:")
    await start(demo.usage_demo, 1, store=store).result()

    events = await store.read_events((await store.list_runs())[0])
    usage = model_usage(events)
    assert usage == [{"model": "demo-model", "input_tokens": 10, "output_tokens": 5}]
    store.close()


async def test_non_reporting_task_records_no_usage() -> None:
    store = SQLiteStore.open(":memory:")
    await start(demo.quiet_demo, 1, store=store).result()

    events = await store.read_events((await store.list_runs())[0])
    assert model_usage(events) == []
    store.close()


async def test_a_failed_attempt_records_the_tokens_it_burned() -> None:
    """KAN-479 (a): the two garbled answers were billed, so they are on the journal."""
    store, _, events = await _drive(succeed_on_attempt=3)

    failed = payload_usage(events, EventType.TASK_ATTEMPT_FAILED)
    assert [entry["attempt"] for entry in failed] == [1, 2]
    assert input_tokens(failed) == TOKENS_PER_ATTEMPT * (1 + 2)
    store.close()


async def test_a_task_that_never_completes_still_records_its_usage() -> None:
    """KAN-479 (b): three attempts, no ``TaskCompleted``, and still a full bill."""
    store, _, events = await _drive(succeed_on_attempt=99)

    assert not [e for e in events if e.type is EventType.TASK_COMPLETED]
    assert [entry["attempt"] for entry in model_usage(events)] == [1, 2, 3]
    assert input_tokens(model_usage(events)) == sum(BILLED) > 0
    store.close()


async def test_the_aggregate_for_a_failed_run_is_the_whole_bill() -> None:
    """KAN-479 (c): "what did this run cost" answers with everything the provider charged."""
    store, _, events = await _drive(succeed_on_attempt=99)

    assert input_tokens(model_usage(events)) == sum(BILLED)
    store.close()


async def test_the_aggregate_for_a_retried_run_counts_every_attempt() -> None:
    """A run that recovers still paid for the attempts it threw away."""
    store, _, events = await _drive(succeed_on_attempt=3)

    assert input_tokens(model_usage(events)) == sum(BILLED)
    assert len(BILLED) == 3
    store.close()


async def test_model_usage_can_be_narrowed_to_work_that_succeeded() -> None:
    """The complete figure is the default; success-only is the explicit opt-in."""
    store, _, events = await _drive(succeed_on_attempt=3)

    completed = model_usage(events, include_failed_attempts=False)
    assert [entry["attempt"] for entry in completed] == [3]
    assert input_tokens(completed) == TOKENS_PER_ATTEMPT * 3
    assert input_tokens(completed) < input_tokens(model_usage(events))
    store.close()


async def test_task_detail_prices_the_attempts_that_failed() -> None:
    """The read API bills per attempt and totals the lot (V6/Studio, ADR-0018 additive)."""
    store, run_id, _ = await _drive(succeed_on_attempt=3)
    detail = await task_detail(store, run_id, "flaky_model_call:0")

    assert input_tokens(detail["usage"]) == sum(BILLED)
    assert [a["attempt"] for a in detail["attempts"]] == [1, 2, 3]
    assert [input_tokens(a["usage"]) for a in detail["attempts"]] == [
        TOKENS_PER_ATTEMPT,
        TOKENS_PER_ATTEMPT * 2,
        TOKENS_PER_ATTEMPT * 3,
    ]
    store.close()


async def test_usage_of_an_abandoned_attempt_is_not_journalled() -> None:
    """A worker death mid-attempt loses that attempt's usage — deliberately.

    Nothing was committed, because the process notionally stopped: the runtime flushes
    usage onto the attempt's own outcome event, and an abandoned attempt has none. Writing
    one anyway would fake durability the runtime did not deliver. The tokens are recovered
    only by the resume re-running the task.
    """
    store = SQLiteStore.open(":memory:")
    handle = start(abandoned_call, 1, store=store)
    with pytest.raises(SimulatedCrash):
        await handle.result()

    events = list(await store.read_events(handle.run_id))
    assert [e.type for e in events if e.type is EventType.TASK_ATTEMPT_STARTED]
    assert model_usage(events) == []
    store.close()
