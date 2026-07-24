"""Integration tests for the timer/event poll loop boundaries (N11, ADR-0021).

Narrowed per ADR-0011 (H3) to the poll-loop and inbox-query boundaries: that the loop
fires only due timers, checks a matching inbox event *before* resolving a due timeout
(the ADR-0021 deliver-then-timeout order), and that a duplicate fire does not
double-resume. All timing is driven by advancing the manual clock — nothing waits on
wall-clock.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from satay.api.decorators import task, workflow
from satay.api.primitives import send_event, sleep, start, wait_for_event
from satay.journal.events import EventType, TimerStatus
from satay.journal.store import SQLiteStore
from satay.testing.clock import ManualClock
from satay.timers import TimerEventWorker

_EXEC: dict[str, int] = {}


@dataclass(frozen=True)
class PLDecision:
    approved: bool


@task()
async def pl_touch(name: str) -> str:
    _EXEC[name] = _EXEC.get(name, 0) + 1
    return name


@workflow
async def pl_short_sleep(value: int) -> str:
    await sleep(timedelta(hours=1))
    return await pl_touch("short")


@workflow
async def pl_long_sleep(value: int) -> str:
    await sleep(timedelta(hours=5))
    return await pl_touch("long")


@workflow
async def pl_timeout_wait(value: int) -> str:
    decision = await wait_for_event(PLDecision, key="k", timeout=timedelta(hours=2))
    return "timed_out" if decision is None else "delivered"


def _reset() -> None:
    _EXEC.clear()


async def test_poll_loop_fires_only_due_timers() -> None:
    _reset()
    clock = ManualClock()
    store = SQLiteStore.open(":memory:")
    short = start(pl_short_sleep, 0, store=store, clock=clock)
    long = start(pl_long_sleep, 0, store=store, clock=clock)
    assert await short.result() is None  # both park on their sleep
    assert await long.result() is None

    worker = TimerEventWorker(store=store, clock=clock)

    clock.advance(3600)  # +1h: only the short sleep is due
    assert await worker.tick() == 1
    assert await short.status() == "completed"
    assert await long.status() == "waiting"

    clock.advance(4 * 3600)  # +5h total: the long sleep is now due
    assert await worker.tick() == 1
    assert await long.status() == "completed"
    store.close()


async def test_poll_loop_checks_event_before_resolving_timeout() -> None:
    """Co-scheduled event + timeout: the event resolves the wait, timeout is discarded.

    The deliver-then-timeout order (ADR-0021): on one tick where the wait's timeout is
    due and a matching inbox event is present, the event wins and the timeout timer is
    discarded rather than fired.
    """
    _reset()
    clock = ManualClock()
    store = SQLiteStore.open(":memory:")
    handle = start(pl_timeout_wait, 0, store=store, clock=clock)
    assert await handle.result() is None  # parks with an event_timeout timer at +2h

    # Advance to the timeout's fire_at AND deliver a matching event on the same tick.
    clock.advance(2 * 3600)
    await send_event(PLDecision(approved=True), key="k", store=store)
    worker = TimerEventWorker(store=store, clock=clock)
    assert await worker.tick() == 1
    assert await handle.result() == "delivered"

    types = [e.type for e in await store.read_events(handle.run_id)]
    assert EventType.EXTERNAL_EVENT_RECEIVED in types
    assert EventType.TIMER_FIRED not in types  # the timeout never fired
    # The timeout timer row was discarded (event-wins), not fired.
    rows = store._conn.execute("SELECT status FROM timers").fetchall()
    assert [r[0] for r in rows] == [TimerStatus.DISCARDED.value]
    store.close()


async def test_duplicate_timer_fire_does_not_double_resume() -> None:
    _reset()
    clock = ManualClock()
    store = SQLiteStore.open(":memory:")
    handle = start(pl_short_sleep, 0, store=store, clock=clock)
    assert await handle.result() is None

    worker = TimerEventWorker(store=store, clock=clock)
    clock.advance(3600)
    assert await worker.tick() == 1  # fires + resumes once
    assert await worker.tick() == 0  # nothing due now (timer FIRED); no re-resume

    events = await store.read_events(handle.run_id)
    assert [e.type for e in events].count(EventType.TIMER_FIRED) == 1
    assert _EXEC["short"] == 1  # the continuation ran exactly once
    store.close()


async def test_timeout_timer_status_after_firing() -> None:
    _reset()
    clock = ManualClock()
    store = SQLiteStore.open(":memory:")
    handle = start(pl_timeout_wait, 0, store=store, clock=clock)
    assert await handle.result() is None

    worker = TimerEventWorker(store=store, clock=clock)
    clock.advance(2 * 3600)
    assert await worker.tick() == 1
    assert await handle.result() == "timed_out"

    rows = store._conn.execute("SELECT status FROM timers").fetchall()
    assert [r[0] for r in rows] == [TimerStatus.FIRED.value]
    store.close()
