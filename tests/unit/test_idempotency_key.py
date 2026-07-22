"""Unit tests for idempotency-key derivation (N13, A4.3, ADR-0006)."""

from __future__ import annotations

from satay.replay.identity import idempotency_key


def test_key_is_stable_across_attempts() -> None:
    """The key excludes attempt/args, so it is identical across physical retries."""
    a = idempotency_key("run-1", "charge", 0)
    b = idempotency_key("run-1", "charge", 0)
    assert a == b


def test_key_is_distinct_across_ordinals() -> None:
    """Two durable calls of the same task differ by ordinal → distinct keys."""
    first = idempotency_key("run-1", "charge", 0)
    second = idempotency_key("run-1", "charge", 1)
    assert first != second


def test_key_is_distinct_across_tasks_and_runs() -> None:
    base = idempotency_key("run-1", "charge", 0)
    assert idempotency_key("run-1", "refund", 0) != base  # different task
    assert idempotency_key("run-2", "charge", 0) != base  # different run


def test_key_is_a_stable_hex_digest() -> None:
    key = idempotency_key("run-1", "charge", 0)
    assert isinstance(key, str)
    assert len(key) == 64  # sha256 hex
    assert int(key, 16) >= 0  # all hex
