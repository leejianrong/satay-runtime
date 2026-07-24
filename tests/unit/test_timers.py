"""Unit tests for the V3 timer/event pure helpers (ADR-0007/0021).

These exercise logic in isolation: the event-type key derivation, timedelta coercion,
the outstanding-wait computation over a synthetic event list, and the ``fire_at =
clock + timedelta`` arithmetic on the manual clock. Store-backed query behaviour (the
due-check and inbox match) is proven at the integration tier.
"""

from __future__ import annotations

from datetime import timedelta

from satay.api.primitives import _as_timedelta, event_type_name
from satay.journal.events import Event, EventType, TimerKind
from satay.testing.clock import ManualClock
from satay.timers import _outstanding_event_waits


class _Sample:
    pass


def test_event_type_name_uses_module_qualname() -> None:
    name = event_type_name(_Sample)
    assert name.endswith("_Sample")
    assert "test_timers" in name
    # A string type name passes through unchanged (so wait/send agree on a custom key).
    assert event_type_name("orders.Approved") == "orders.Approved"


def test_as_timedelta_coerces_seconds_and_passes_through() -> None:
    assert _as_timedelta(5) == timedelta(seconds=5)
    assert _as_timedelta(1.5) == timedelta(seconds=1.5)
    delta = timedelta(hours=2)
    assert _as_timedelta(delta) is delta


def test_fire_at_is_clock_plus_timedelta() -> None:
    clock = ManualClock()
    base = clock.now()
    assert clock.now() + timedelta(hours=1) == base + timedelta(hours=1)
    clock.advance(1800)  # 30 minutes
    assert clock.now() == base + timedelta(minutes=30)


def _wait_started(identity: str, event_type: str, key: str | None) -> Event:
    return Event(
        run_id="r",
        type=EventType.EVENT_WAIT_STARTED,
        payload={"identity": identity, "event_type": event_type, "key": key},
    )


def test_outstanding_event_waits_returns_only_unresolved() -> None:
    events = [
        _wait_started("event#0", "Approved", "k0"),
        Event(
            run_id="r",
            type=EventType.EXTERNAL_EVENT_RECEIVED,
            payload={"identity": "event#0"},
        ),
        _wait_started("event#1", "Approved", "k1"),  # still open
    ]
    outstanding = _outstanding_event_waits(events)
    assert outstanding == [("event#1", "Approved", "k1")]


def test_outstanding_event_waits_resolved_by_timeout_fire() -> None:
    events = [
        _wait_started("event#0", "Approved", "k0"),
        Event(
            run_id="r",
            type=EventType.TIMER_FIRED,
            payload={"identity": "event#0", "kind": TimerKind.EVENT_TIMEOUT.value},
        ),
    ]
    assert _outstanding_event_waits(events) == []
