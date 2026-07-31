"""E2E: a recorded ``TimerCreated`` always has a ``timers`` row after resume (KAN-443).

``TimerCreated`` is committed to the journal *before* the derived ``timers`` row is
inserted, so a crash in that window left a journal that had already created the timer on
a run that had no row for the poll loop to fire. The resume path then saw the recorded
``TimerCreated``, treated the timer as already created, skipped the block, and re-parked —
so **nothing ever inserted the row**. The run sat in ``waiting`` forever, unrecoverably.

The journal is the single source of truth and the ``timers`` table is derived from it
(ADR-0004), so resume now **repairs** the derived row from the recorded ``TimerCreated``
payload, which deliberately carries ``timer_id``, ``kind``, ``identity`` and ``fire_at``.
This is the KAN-394 precedent (idempotent terminal append) applied to the other side
table. The repair is idempotent on ``timer_id``: resuming any number of times converges on
exactly one row per recorded timer, and the ``wait_for_event`` timeout timer no longer
appends a second ``TimerCreated`` for a wait it had already created one for (ADR-0021 —
the timeout deadline must stay where it was first recorded, not slide to resume time).

Driven through the primary seam (ADR-0011): the public ``satay.start`` API, an in-memory
``SQLiteStore``, the ``ManualClock``, and the ``FaultInjector`` crash-after-named-event
hook. Only observable outcomes are asserted — the journal, the run status, the timer rows,
the result, and the demo execution-count marker.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from satay import demo
from satay.api.primitives import send_event, start
from satay.demo import REVIEW_KEY, ReviewDecision
from satay.journal.events import EventType, RunStatus, TimerRecord
from satay.journal.store import SQLiteStore
from satay.testing.clock import ManualClock
from satay.testing.faults import FaultInjector, SimulatedCrash
from satay.timers import TimerEventWorker

_HOUR = 3600.0

#: Far enough past any demo ``fire_at`` that ``due_timers`` returns every pending row.
_FOREVER = timedelta(days=365)


@pytest.fixture(autouse=True)
def _reset_marker() -> None:
    demo.reset_executions()


async def _pending_timers(store: SQLiteStore, run_id: str, clock: ManualClock) -> list[TimerRecord]:
    """Every pending ``timers`` row for ``run_id``, regardless of how far off ``fire_at`` is."""
    due = await store.due_timers(clock.now() + _FOREVER)
    return [t for t in due if t.run_id == run_id]


async def _timer_created_identities(store: SQLiteStore, run_id: str) -> list[str]:
    """The ``identity`` of every recorded ``TimerCreated``, in journal order (duplicates kept)."""
    events = await store.read_events(run_id)
    return [e.payload["identity"] for e in events if e.type is EventType.TIMER_CREATED]


async def _assert_no_orphan_rows(store: SQLiteStore, run_id: str, clock: ManualClock) -> None:
    """The mirror invariant: no ``timers`` row may exist that the journal does not justify.

    Every write orders the journal event *before* the derived row, so a row without its
    ``TimerCreated`` is unreachable by construction. Asserted at every phase so the
    KAN-443 repair cannot fix one direction by breaking the other.
    """
    recorded = set(await _timer_created_identities(store, run_id))
    for timer in await _pending_timers(store, run_id, clock):
        assert timer.identity in recorded, (
            f"orphan timers row {timer.timer_id} for identity {timer.identity!r} "
            "has no TimerCreated in the journal"
        )


# -- durable sleep ---------------------------------------------------------------


async def test_crash_between_timer_created_and_the_timers_row_stays_recoverable() -> None:
    """The KAN-443 headline: a crash in that window must not hang the run forever."""
    clock = ManualClock()
    store = SQLiteStore.open(":memory:")
    injector = FaultInjector()
    injector.crash_after("TimerCreated")  # dies after the event commits, before the row

    handle = start(demo.sleep_demo, 1, store=store, injector=injector, clock=clock)
    with pytest.raises(SimulatedCrash):
        await handle.result()

    # The journal has created the timer, but the crash beat the derived row.
    assert await _timer_created_identities(store, handle.run_id) == ["sleep#0"]
    assert await _pending_timers(store, handle.run_id, clock) == []
    await _assert_no_orphan_rows(store, handle.run_id, clock)
    assert demo.execution_count("step_one") == 1

    # Resume: the derived row is repaired from the recorded TimerCreated payload.
    resumed = start(demo.sleep_demo, 1, run_id=handle.run_id, store=store, clock=clock)
    assert await resumed.result() is None  # parks again on the same sleep
    assert await resumed.status() == RunStatus.WAITING.value

    rows = await _pending_timers(store, handle.run_id, clock)
    assert len(rows) == 1
    assert rows[0].identity == "sleep#0"
    # Repaired from the journal, so the deadline is the originally recorded one — the
    # sleep does not silently restart from resume time.
    assert rows[0].fire_at == clock.now() + timedelta(hours=1)
    # No second TimerCreated: the journal records one creation per timer (ADR-0004).
    assert await _timer_created_identities(store, handle.run_id) == ["sleep#0"]
    await _assert_no_orphan_rows(store, handle.run_id, clock)

    # The run is genuinely recoverable: the poll loop now has a timer to fire.
    worker = TimerEventWorker(store=store, clock=clock)
    assert await worker.tick() == 0  # not due yet
    clock.advance(_HOUR)
    assert await worker.tick() == 1

    assert await resumed.status() == RunStatus.COMPLETED.value
    assert await resumed.result() == 4  # step_two(step_one(1)) = (1+1)*2
    assert demo.execution_count("step_one") == 1  # reused from the journal, not re-run
    assert demo.execution_count("step_two") == 1
    store.close()


async def test_repairing_the_timers_row_twice_creates_only_one_row() -> None:
    """The repair is idempotent: resuming repeatedly converges on one row per timer."""
    clock = ManualClock()
    store = SQLiteStore.open(":memory:")
    injector = FaultInjector()
    injector.crash_after("TimerCreated")

    handle = start(demo.sleep_demo, 1, store=store, injector=injector, clock=clock)
    with pytest.raises(SimulatedCrash):
        await handle.result()

    for _ in range(3):
        resumed = start(demo.sleep_demo, 1, run_id=handle.run_id, store=store, clock=clock)
        assert await resumed.result() is None
        assert len(await _pending_timers(store, handle.run_id, clock)) == 1
        assert await _timer_created_identities(store, handle.run_id) == ["sleep#0"]
        await _assert_no_orphan_rows(store, handle.run_id, clock)

    # One row, one creation, and it still fires exactly once.
    worker = TimerEventWorker(store=store, clock=clock)
    clock.advance(_HOUR)
    assert await worker.tick() == 1
    assert await worker.tick() == 0  # nothing left pending

    events = await store.read_events(handle.run_id)
    fired = [e for e in events if e.type is EventType.TIMER_FIRED]
    assert len(fired) == 1
    assert (
        await start(demo.sleep_demo, 1, run_id=handle.run_id, store=store, clock=clock).result()
        == 4
    )
    store.close()


async def test_a_healthy_sleep_park_is_unaffected_by_the_repair() -> None:
    """No-crash control: the repair must not disturb the ordinary park/fire path."""
    clock = ManualClock()
    store = SQLiteStore.open(":memory:")

    handle = start(demo.sleep_demo, 1, store=store, clock=clock)
    assert await handle.result() is None
    assert len(await _pending_timers(store, handle.run_id, clock)) == 1

    # A resume that is not repairing anything still adds no duplicate row.
    resumed = start(demo.sleep_demo, 1, run_id=handle.run_id, store=store, clock=clock)
    assert await resumed.result() is None
    assert len(await _pending_timers(store, handle.run_id, clock)) == 1
    assert await _timer_created_identities(store, handle.run_id) == ["sleep#0"]

    worker = TimerEventWorker(store=store, clock=clock)
    clock.advance(_HOUR)
    assert await worker.tick() == 1
    assert await resumed.result() == 4
    store.close()


# -- wait_for_event timeout (the structurally identical second call site) ---------


async def test_crash_after_the_event_timeout_timer_created_keeps_one_timer() -> None:
    """The ``wait_for_event`` timeout timer has the same shape and the same repair.

    Pre-fix the ``EventWaitStarted`` guard (not the ``TimerCreated`` guard) gated this
    block, so a crash after ``TimerCreated`` re-entered it on resume and appended a
    *second* ``TimerCreated`` with a fresh ``timer_id`` — a duplicate creation whose
    ``fire_at`` had slid forward to resume time (ADR-0004 / ADR-0021).
    """
    clock = ManualClock()
    store = SQLiteStore.open(":memory:")
    injector = FaultInjector()
    injector.crash_after("TimerCreated")

    handle = start(demo.review_timeout_demo, 1, store=store, injector=injector, clock=clock)
    with pytest.raises(SimulatedCrash):
        await handle.result()

    assert await _timer_created_identities(store, handle.run_id) == ["event#0"]
    assert await _pending_timers(store, handle.run_id, clock) == []
    await _assert_no_orphan_rows(store, handle.run_id, clock)
    # The crash landed before EventWaitStarted, so the wait is not yet recorded either.
    events = await store.read_events(handle.run_id)
    assert EventType.EVENT_WAIT_STARTED not in [e.type for e in events]

    # Resume: one repaired row, one recorded creation, the original deadline.
    resumed = start(demo.review_timeout_demo, 1, run_id=handle.run_id, store=store, clock=clock)
    assert await resumed.result() is None
    assert await resumed.status() == RunStatus.WAITING.value

    assert await _timer_created_identities(store, handle.run_id) == ["event#0"]
    rows = await _pending_timers(store, handle.run_id, clock)
    assert len(rows) == 1
    assert rows[0].identity == "event#0"
    assert rows[0].fire_at == clock.now() + timedelta(hours=2)
    await _assert_no_orphan_rows(store, handle.run_id, clock)
    # The wait itself is now recorded, so the poll loop can also deliver an event to it.
    events = await store.read_events(handle.run_id)
    assert EventType.EVENT_WAIT_STARTED in [e.type for e in events]

    # The timeout still resolves the wait on time.
    worker = TimerEventWorker(store=store, clock=clock)
    clock.advance(2 * _HOUR)
    assert await worker.tick() == 1
    assert await resumed.status() == RunStatus.COMPLETED.value
    assert await resumed.result() == "timed_out"
    store.close()


async def test_repaired_event_timeout_still_loses_to_a_delivered_event() -> None:
    """ADR-0021 event-wins survives the repair: a delivered event beats the repaired timer."""
    clock = ManualClock()
    store = SQLiteStore.open(":memory:")
    injector = FaultInjector()
    injector.crash_after("TimerCreated")

    handle = start(demo.review_timeout_demo, 1, store=store, injector=injector, clock=clock)
    with pytest.raises(SimulatedCrash):
        await handle.result()

    resumed = start(demo.review_timeout_demo, 1, run_id=handle.run_id, store=store, clock=clock)
    assert await resumed.result() is None
    assert len(await _pending_timers(store, handle.run_id, clock)) == 1

    # Deliver the decision and co-schedule the repaired timeout in the same tick.
    await send_event(ReviewDecision(approved=True), key=REVIEW_KEY, store=store)
    clock.advance(2 * _HOUR)
    worker = TimerEventWorker(store=store, clock=clock)
    await worker.tick()

    assert await resumed.status() == RunStatus.COMPLETED.value
    assert await resumed.result() == "approved"
    events = await store.read_events(handle.run_id)
    types = [e.type for e in events]
    assert EventType.EXTERNAL_EVENT_RECEIVED in types
    assert EventType.TIMER_FIRED not in types  # the timeout was discarded, not fired
    store.close()


async def test_crash_after_event_wait_started_does_not_duplicate_the_timer() -> None:
    """The adjacent window (row written, ``WorkflowWaiting`` not yet) stays single-timer."""
    clock = ManualClock()
    store = SQLiteStore.open(":memory:")
    injector = FaultInjector()
    injector.crash_after("EventWaitStarted")  # after TimerCreated *and* the row

    handle = start(demo.review_timeout_demo, 1, store=store, injector=injector, clock=clock)
    with pytest.raises(SimulatedCrash):
        await handle.result()

    assert await _timer_created_identities(store, handle.run_id) == ["event#0"]
    assert len(await _pending_timers(store, handle.run_id, clock)) == 1
    await _assert_no_orphan_rows(store, handle.run_id, clock)

    resumed = start(demo.review_timeout_demo, 1, run_id=handle.run_id, store=store, clock=clock)
    assert await resumed.result() is None
    assert await _timer_created_identities(store, handle.run_id) == ["event#0"]
    assert len(await _pending_timers(store, handle.run_id, clock)) == 1

    worker = TimerEventWorker(store=store, clock=clock)
    clock.advance(2 * _HOUR)
    assert await worker.tick() == 1
    assert await resumed.result() == "timed_out"
    store.close()
