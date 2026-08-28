"""Unit tests for the journal event envelope, the ordinal counter, and the marker."""

from __future__ import annotations

from datetime import UTC, datetime

from satay.journal.codec import from_json, to_json
from satay.journal.events import CallStatus, Event, EventType, RunStatus
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


def test_call_status_is_a_strenum_and_old_string_comparisons_still_work() -> None:
    """CallStatus (ADR-0038) follows the RunStatus/KAN-524 precedent: a StrEnum, so
    ``== "completed"`` keeps working for every existing consumer, while ``is
    CallStatus.COMPLETED`` is now available and typo-proof."""
    assert CallStatus.RUNNING == "running"
    assert CallStatus.COMPLETED == "completed"
    assert CallStatus.FAILED == "failed"
    assert CallStatus("completed") is CallStatus.COMPLETED


def test_call_status_also_covers_a_child_calls_run_status_values() -> None:
    """A ``start_child`` call's status mirrors its own child run's RunStatus, so
    CallStatus has to accept every RunStatus value too, plus UNKNOWN for the
    child-run-not-found fallback — not just the three task-call values."""
    assert CallStatus.WAITING == RunStatus.WAITING
    assert CallStatus.CANCELLED == RunStatus.CANCELLED
    assert CallStatus("unknown") is CallStatus.UNKNOWN


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


def test_task_failed_renders_its_call_identity_and_error() -> None:
    """A collected failure names the item it belongs to (KAN-957).

    ``TaskFailed`` is the terminal verdict on one durable call (ADR-0027), so the line
    has to carry the same ``task=``/``key=`` identity its attempt lines carry — a bare
    type line strands the verdict where two fan-out items are failing side by side.
    """
    events = [
        Event(
            run_id="r1",
            seq=1,
            type=EventType.TASK_ATTEMPT_FAILED,
            payload={
                "task_name": "draft",
                "key": "c-refund",
                "attempt": 2,
                "error": {"type": "MalformedResponseError", "message": "no REPLY"},
            },
        ),
        Event(
            run_id="r1",
            seq=2,
            type=EventType.TASK_FAILED,
            payload={
                "task_name": "draft",
                "key": "c-refund",
                "error": {
                    "type": "MalformedResponseError",
                    "message": "no REPLY",
                    "traceback": "Traceback (most recent call last):\n  ...\n",
                },
            },
        ),
    ]
    line = next(
        line for line in render_timeline(events, run_id="r1").splitlines() if "TaskFailed" in line
    )
    assert "task=draft" in line
    assert "key=c-refund" in line
    assert "error=MalformedResponseError: no REPLY" in line
    # No `attempt=`: the verdict covers the whole call, not one try.
    assert "attempt=" not in line
    # The traceback stays off the timeline. Only the single terminal WorkflowFailed
    # prints one; a collect run can hold many TaskFailed events and would flood.
    assert "Traceback" not in render_timeline(events, run_id="r1")


def test_task_failed_falls_back_to_ordinal_for_an_unkeyed_call() -> None:
    events = [
        Event(
            run_id="r1",
            seq=1,
            type=EventType.TASK_FAILED,
            payload={
                "task_name": "judge",
                "ordinal": 0,
                "error": {"type": "TimeoutError", "message": "took too long"},
            },
        )
    ]
    line = render_timeline(events, run_id="r1").splitlines()[1]
    assert "task=judge" in line
    assert "ordinal=0" in line
    assert "error=TimeoutError: took too long" in line
