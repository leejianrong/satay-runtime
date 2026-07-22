"""Unit tests for the retry backoff schedule (N10, ADR-0006/0011)."""

from __future__ import annotations

import pytest

from satay.executor import (
    BACKOFF_BASE_SECONDS,
    BACKOFF_CAP_SECONDS,
    backoff_ceiling,
    backoff_delay,
)
from satay.testing.rng import SeededRng


def test_ceiling_is_exponential_and_capped() -> None:
    assert backoff_ceiling(1) == BACKOFF_BASE_SECONDS  # 1s
    assert backoff_ceiling(2) == 2.0
    assert backoff_ceiling(3) == 4.0
    assert backoff_ceiling(4) == 8.0
    # Far-out failures are clamped to the cap.
    assert backoff_ceiling(20) == BACKOFF_CAP_SECONDS


def test_ceiling_rejects_non_positive_failure_index() -> None:
    with pytest.raises(ValueError, match="1-based"):
        backoff_ceiling(0)


def test_delay_stays_within_exponential_base_and_cap_bounds() -> None:
    rng = SeededRng(1234)
    for failure in range(1, 12):
        delay = backoff_delay(failure, rng)
        assert 0.0 <= delay <= backoff_ceiling(failure)
        assert delay <= BACKOFF_CAP_SECONDS


def test_schedule_is_reproducible_under_the_seeded_rng() -> None:
    a = [backoff_delay(f, SeededRng(1234)) for f in range(1, 6)]
    b = [backoff_delay(f, SeededRng(1234)) for f in range(1, 6)]
    assert a == b
    # A different seed yields a different (still-bounded) schedule.
    c = [backoff_delay(f, SeededRng(9999)) for f in range(1, 6)]
    assert c != a
