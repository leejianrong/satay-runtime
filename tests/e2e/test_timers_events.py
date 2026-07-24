"""End-to-end acceptance tests for SLICE V3 — timers and events (the Demo).

Driven through the primary seam (ADR-0011): the public ``satay.start`` /
``send_event`` API, a ``:memory:`` ``SQLiteStore``, the ``FaultInjector`` crash hook,
and the ``ManualClock``. Every sleep/timeout is driven by advancing virtual time — no
test waits on wall-clock. The release-while-waiting and resume behaviour is proven
observably: a parked run has no live frame, and its graceful wake writes no
``WorkflowResumed`` and shows no ⚡ (ADR-0009/Q52).
"""

from __future__ import annotations

import pytest

from satay import demo
from satay.api.primitives import send_event, start
from satay.demo import REVIEW_KEY, ReviewDecision
from satay.journal.events import EventType, RunStatus
from satay.journal.store import SQLiteStore
from satay.journal.timeline import interruption_seqs
from satay.testing.clock import ManualClock
from satay.testing.faults import FaultInjector, SimulatedCrash
from satay.timers import TimerEventWorker

_HOUR = 3600.0


@pytest.fixture(autouse=True)
def _reset_marker() -> None:
    demo.reset_executions()


def _types(events: object) -> list[EventType]:
    return [e.type for e in events]  # type: ignore[attr-defined]


# -- durable sleep ---------------------------------------------------------------


async def test_durable_sleep_parks_then_resumes_on_timer() -> None:
    clock = ManualClock()
    store = SQLiteStore.open(":memory:")
    handle = start(demo.sleep_demo, 1, store=store, clock=clock)

    # First drive parks on the sleep: no live frame, no terminal outcome yet.
    assert await handle.result() is None
    assert await handle.status() == RunStatus.WAITING.value
    assert demo.execution_count("step_one") == 1  # ran before the park
    assert demo.execution_count("step_two") == 0  # not reached yet

    events = await store.read_events(handle.run_id)
    types = _types(events)
    assert EventType.TIMER_CREATED in types
    assert EventType.WORKFLOW_WAITING in types
    assert EventType.WORKFLOW_COMPLETED not in types

    worker = TimerEventWorker(store=store, clock=clock)
    assert await worker.tick() == 0  # nothing due yet (fire_at is +1h)

    clock.advance(_HOUR)  # advance past fire_at
    assert await worker.tick() == 1  # timer fires, run re-driven to completion

    assert await handle.status() == RunStatus.COMPLETED.value
    assert await handle.result() == 4  # step_two(step_one(1)) = (1+1)*2
    assert demo.execution_count("step_one") == 1  # reused on the wake, not re-run
    assert demo.execution_count("step_two") == 1

    events = await store.read_events(handle.run_id)
    assert EventType.TIMER_FIRED in _types(events)
    # A graceful wake writes no WorkflowResumed and shows no ⚡ (ADR-0009/Q52).
    assert EventType.WORKFLOW_RESUMED not in _types(events)
    assert interruption_seqs(events) == set()
    store.close()


async def test_durable_sleep_survives_a_crash_while_parked() -> None:
    """A crash after WorkflowWaiting loses nothing: the timer row + journal are durable."""
    clock = ManualClock()
    store = SQLiteStore.open(":memory:")
    injector = FaultInjector()
    injector.crash_after("WorkflowWaiting")  # die right after the park is recorded

    handle = start(demo.sleep_demo, 1, store=store, clock=clock, injector=injector)
    with pytest.raises(SimulatedCrash):
        await handle.result()
    assert demo.execution_count("step_one") == 1  # ran before the crash

    # The park is durable: TimerCreated + WorkflowWaiting are on the journal.
    events = await store.read_events(handle.run_id)
    assert EventType.TIMER_CREATED in _types(events)
    assert EventType.WORKFLOW_WAITING in _types(events)

    # A fresh worker (no crash armed) fires the timer and drives to completion.
    clock.advance(_HOUR)
    worker = TimerEventWorker(store=store, clock=clock)
    assert await worker.tick() == 1

    assert await handle.status() == RunStatus.COMPLETED.value
    assert await handle.result() == 4
    assert demo.execution_count("step_one") == 1  # reused across the crash
    assert demo.execution_count("step_two") == 1
    # A crash while parked is an ordinary parked wake — still no ⚡ (ADR-0009/Q52).
    events = await store.read_events(handle.run_id)
    assert EventType.WORKFLOW_RESUMED not in _types(events)
    assert interruption_seqs(events) == set()
    store.close()


# -- event wait ------------------------------------------------------------------


async def test_event_wait_blocks_then_resumes_on_delivery() -> None:
    clock = ManualClock()
    store = SQLiteStore.open(":memory:")
    handle = start(demo.review_demo, 0, store=store, clock=clock)

    assert await handle.result() is None  # parks on wait_for_event
    assert await handle.status() == RunStatus.WAITING.value
    events = await store.read_events(handle.run_id)
    assert EventType.EVENT_WAIT_STARTED in _types(events)
    assert EventType.WORKFLOW_WAITING in _types(events)

    await send_event(ReviewDecision(approved=True, reviewer="alice"), key=REVIEW_KEY, store=store)
    worker = TimerEventWorker(store=store, clock=clock)
    assert await worker.tick() == 1

    assert await handle.status() == RunStatus.COMPLETED.value
    assert await handle.result() == "approved"

    events = await store.read_events(handle.run_id)
    assert EventType.EXTERNAL_EVENT_RECEIVED in _types(events)
    assert EventType.WORKFLOW_RESUMED not in _types(events)  # graceful wake, no ⚡
    assert interruption_seqs(events) == set()
    # The delivered event was consumed out of the inbox.
    assert await store.list_inbox_events(include_consumed=False) == []
    store.close()


async def test_event_delivered_before_the_wait_is_matched_from_inbox() -> None:
    clock = ManualClock()
    store = SQLiteStore.open(":memory:")

    # Deliver the event BEFORE the workflow ever reaches its wait.
    await send_event(ReviewDecision(approved=False), key=REVIEW_KEY, store=store)

    handle = start(demo.review_demo, 0, store=store, clock=clock)
    result = await handle.result()  # matches the buffered event immediately; never parks

    assert result == "rejected"
    assert await handle.status() == RunStatus.COMPLETED.value
    events = await store.read_events(handle.run_id)
    assert EventType.EXTERNAL_EVENT_RECEIVED in _types(events)
    assert EventType.WORKFLOW_WAITING not in _types(events)  # never parked
    assert await store.list_inbox_events(include_consumed=False) == []
    store.close()


# -- timeout ---------------------------------------------------------------------


async def test_wait_timeout_resolves_via_timer_path() -> None:
    clock = ManualClock()
    store = SQLiteStore.open(":memory:")
    handle = start(demo.review_timeout_demo, 0, store=store, clock=clock)

    assert await handle.result() is None  # parks with an event_timeout timer at +2h
    events = await store.read_events(handle.run_id)
    assert EventType.TIMER_CREATED in _types(events)

    clock.advance(2 * _HOUR)  # advance past the timeout's fire_at
    worker = TimerEventWorker(store=store, clock=clock)
    assert await worker.tick() == 1

    assert await handle.result() == "timed_out"
    events = await store.read_events(handle.run_id)
    assert EventType.TIMER_FIRED in _types(events)
    assert EventType.EXTERNAL_EVENT_RECEIVED not in _types(events)
    store.close()


async def test_event_wins_a_simultaneously_due_timeout() -> None:
    """Event and timeout both due on one tick: the event wins, timeout is discarded."""
    clock = ManualClock()
    store = SQLiteStore.open(":memory:")
    handle = start(demo.review_timeout_demo, 0, store=store, clock=clock)
    assert await handle.result() is None

    # Co-schedule: advance to the timeout fire_at AND deliver a matching event.
    clock.advance(2 * _HOUR)
    await send_event(ReviewDecision(approved=True), key=REVIEW_KEY, store=store)
    worker = TimerEventWorker(store=store, clock=clock)
    assert await worker.tick() == 1

    assert await handle.result() == "approved"  # the event won
    events = await store.read_events(handle.run_id)
    assert EventType.EXTERNAL_EVENT_RECEIVED in _types(events)
    assert EventType.TIMER_FIRED not in _types(events)  # timeout never fired
    store.close()


# -- FIFO + inbox disposition ----------------------------------------------------


async def test_buffered_events_are_consumed_fifo() -> None:
    clock = ManualClock()
    store = SQLiteStore.open(":memory:")
    handle = start(demo.review_demo, 0, store=store, clock=clock)
    assert await handle.result() is None  # parks

    # Two matching events buffered; FIFO means the FIRST (approved) is consumed.
    await send_event(ReviewDecision(approved=True, reviewer="first"), key=REVIEW_KEY, store=store)
    await send_event(ReviewDecision(approved=False, reviewer="second"), key=REVIEW_KEY, store=store)

    worker = TimerEventWorker(store=store, clock=clock)
    assert await worker.tick() == 1
    assert await handle.result() == "approved"  # first-arrived event won (FIFO)

    # The second event stays buffered for a later wait.
    pending = await store.list_inbox_events(include_consumed=False)
    assert len(pending) == 1
    assert pending[0].payload_ref["fields"]["reviewer"] == "second"
    store.close()


async def test_unmatched_inbox_event_persists_until_run_end() -> None:
    """An event with a non-matching key is never consumed; its disposition is asserted."""
    clock = ManualClock()
    store = SQLiteStore.open(":memory:")

    # An event on a different key that this workflow never waits for.
    await send_event(ReviewDecision(approved=True), key="unrelated-key", store=store)

    handle = start(demo.review_demo, 0, store=store, clock=clock)
    assert await handle.result() is None  # parks on REVIEW_KEY

    await send_event(ReviewDecision(approved=True), key=REVIEW_KEY, store=store)
    worker = TimerEventWorker(store=store, clock=clock)
    assert await worker.tick() == 1
    assert await handle.result() == "approved"

    # At run end the unmatched event is still buffered (unconsumed) — never lost.
    pending = await store.list_inbox_events(include_consumed=False)
    assert [e.key for e in pending] == ["unrelated-key"]
    store.close()


# -- idempotent firing -----------------------------------------------------------


async def test_timer_firing_is_idempotent_no_double_resume() -> None:
    clock = ManualClock()
    store = SQLiteStore.open(":memory:")
    handle = start(demo.sleep_demo, 1, store=store, clock=clock)
    assert await handle.result() is None

    clock.advance(_HOUR)
    worker = TimerEventWorker(store=store, clock=clock)
    assert await worker.tick() == 1  # fires once
    assert await worker.tick() == 0  # duplicate tick: nothing to fire, no re-resume

    events = await store.read_events(handle.run_id)
    assert _types(events).count(EventType.TIMER_FIRED) == 1
    assert _types(events).count(EventType.WORKFLOW_COMPLETED) == 1
    assert demo.execution_count("step_two") == 1  # continuation ran exactly once
    store.close()
