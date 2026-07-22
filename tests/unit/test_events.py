"""Unit tests for the journal event envelope, the ordinal counter, and the marker."""

from __future__ import annotations

from datetime import UTC, datetime

from satay.journal.codec import from_json, to_json
from satay.journal.events import Event, EventType
from satay.journal.timeline import interruption_seqs, render_timeline
from satay.replay.identity import IdentityResolver


def test_event_envelope_has_all_required_fields() -> None:
    event = Event(
        run_id="r1",
        type=EventType.WORKFLOW_CREATED,
        payload={"workflow_name": "demo"},
        ts=datetime(2026, 7, 22, tzinfo=UTC),
    )
    assert event.run_id == "r1"
    assert event.type is EventType.WORKFLOW_CREATED
    assert event.seq == 0  # unallocated until appended
    assert event.event_id  # auto-allocated, globally unique
    assert event.ts.tzinfo is UTC


def test_event_envelope_serialises_and_deserialises() -> None:
    event = Event(run_id="r1", type=EventType.TASK_COMPLETED, payload={"ordinal": 2})
    text = to_json(dict(event.payload))
    assert from_json(text) == {"ordinal": 2}


def test_with_seq_stamps_allocated_seq() -> None:
    event = Event(run_id="r1", type=EventType.WORKFLOW_CREATED)
    stamped = event.with_seq(5)
    assert stamped.seq == 5
    assert stamped.event_id == event.event_id
    assert event.seq == 0  # original is frozen/unchanged


def test_ordinal_counter_increments_independently_per_task() -> None:
    resolver = IdentityResolver()
    assert resolver.next("a").ordinal == 0
    assert resolver.next("a").ordinal == 1
    assert resolver.next("b").ordinal == 0
    assert resolver.next("a").ordinal == 2
    assert resolver.next("b").ordinal == 1


def _ev(seq: int, etype: EventType) -> Event:
    return Event(run_id="r1", type=etype, seq=seq)


def test_interruption_marker_detects_resume_point() -> None:
    events = [
        _ev(1, EventType.WORKFLOW_CREATED),
        _ev(2, EventType.TASK_COMPLETED),
        _ev(3, EventType.WORKFLOW_RESUMED),
        _ev(4, EventType.WORKFLOW_COMPLETED),
    ]
    assert interruption_seqs(events) == {3}
    rendered = render_timeline(events, run_id="r1")
    resume_line = next(line for line in rendered.splitlines() if "WorkflowResumed" in line)
    assert resume_line.startswith("⚡")


def test_no_interruption_marker_without_resume() -> None:
    events = [_ev(1, EventType.WORKFLOW_CREATED), _ev(2, EventType.WORKFLOW_COMPLETED)]
    assert interruption_seqs(events) == set()
