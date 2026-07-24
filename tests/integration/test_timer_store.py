"""Integration tests for the V3 store queries: due timers and inbox matching (N11).

Boundary-only (ADR-0011 H3): rows are written directly to a temp/``:memory:`` store
and the ``due_timers`` / ``match_inbox_event`` queries are asserted, proving the
"due as of T" comparison and the ``(type, key)`` FIFO match in isolation from the poll
loop.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from satay.journal.events import InboxEventRecord, TimerKind, TimerRecord, TimerStatus
from satay.journal.store import SQLiteStore

_T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _timer(
    timer_id: str, fire_at: datetime, *, status: TimerStatus = TimerStatus.PENDING
) -> TimerRecord:
    return TimerRecord(
        timer_id=timer_id,
        run_id="r1",
        kind=TimerKind.SLEEP,
        identity=f"sleep#{timer_id}",
        fire_at=fire_at,
        status=status,
        created_at=_T0,
    )


async def test_due_timers_returns_only_due_pending_ordered() -> None:
    store = SQLiteStore.open(":memory:")
    await store.add_timer(_timer("a", _T0 + timedelta(hours=1)))
    await store.add_timer(_timer("b", _T0 + timedelta(hours=2)))
    await store.add_timer(_timer("c", _T0 + timedelta(hours=1), status=TimerStatus.FIRED))

    # Before any fire_at: nothing due.
    assert await store.due_timers(_T0) == []
    # At 1h: only 'a' is due ('c' is already fired, 'b' not yet).
    due = await store.due_timers(_T0 + timedelta(hours=1))
    assert [t.timer_id for t in due] == ["a"]
    # At 2h: 'a' then 'b', earliest fire_at first.
    due = await store.due_timers(_T0 + timedelta(hours=2))
    assert [t.timer_id for t in due] == ["a", "b"]
    store.close()


async def test_set_timer_status_removes_it_from_due() -> None:
    store = SQLiteStore.open(":memory:")
    await store.add_timer(_timer("a", _T0))
    assert [t.timer_id for t in await store.due_timers(_T0)] == ["a"]
    await store.set_timer_status("a", TimerStatus.FIRED)
    assert await store.due_timers(_T0) == []
    store.close()


def _inbox(
    event_type: str, key: str | None, received_at: datetime, payload: object
) -> InboxEventRecord:
    return InboxEventRecord(
        event_type=event_type,
        key=key,
        payload_ref=payload,
        received_at=received_at,
    )


async def test_match_inbox_event_by_type_and_key_fifo() -> None:
    store = SQLiteStore.open(":memory:")
    first = await store.add_inbox_event(_inbox("Approved", "k1", _T0, {"n": 1}))
    await store.add_inbox_event(_inbox("Approved", "k1", _T0 + timedelta(seconds=1), {"n": 2}))
    await store.add_inbox_event(_inbox("Approved", "other", _T0, {"n": 3}))

    match = await store.match_inbox_event("Approved", "k1")
    assert match is not None
    assert match.payload_ref == {"n": 1}  # earliest by received_at (FIFO)
    assert match.row_id == first.row_id

    # A non-matching key/type resolves nothing and leaves events pending.
    assert await store.match_inbox_event("Approved", "missing") is None
    assert await store.match_inbox_event("Other", "k1") is None
    store.close()


async def test_match_inbox_event_null_key() -> None:
    store = SQLiteStore.open(":memory:")
    await store.add_inbox_event(_inbox("Ping", None, _T0, {"n": 1}))
    await store.add_inbox_event(_inbox("Ping", "keyed", _T0, {"n": 2}))
    match = await store.match_inbox_event("Ping", None)
    assert match is not None
    assert match.key is None
    assert match.payload_ref == {"n": 1}
    store.close()


async def test_consume_marks_event_and_advances_fifo() -> None:
    store = SQLiteStore.open(":memory:")
    a = await store.add_inbox_event(_inbox("Approved", "k1", _T0, {"n": 1}))
    await store.add_inbox_event(_inbox("Approved", "k1", _T0 + timedelta(seconds=1), {"n": 2}))

    await store.consume_inbox_event(a.row_id)
    match = await store.match_inbox_event("Approved", "k1")
    assert match is not None
    assert match.payload_ref == {"n": 2}  # first was consumed; FIFO advances to the next

    pending = await store.list_inbox_events(include_consumed=False)
    assert [e.payload_ref for e in pending] == [{"n": 2}]
    store.close()
