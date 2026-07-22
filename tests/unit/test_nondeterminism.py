"""Unit tests for the public error payloads (N9/A10.2, ADR-0003/0006)."""

from __future__ import annotations

from satay.replay.nondeterminism import EffectSafetyError, NondeterminismError


def test_nondeterminism_error_carries_expected_versus_actual() -> None:
    err = NondeterminismError(position=2, expected="charge", actual="refund")
    assert err.position == 2
    assert err.expected == "charge"
    assert err.actual == "refund"
    message = str(err)
    assert "position 2" in message
    assert "charge" in message  # expected
    assert "refund" in message  # actual
    assert isinstance(err, RuntimeError)


def test_effect_safety_error_names_the_task() -> None:
    err = EffectSafetyError("send_email")
    assert err.task_name == "send_email"
    assert "send_email" in str(err)
    assert "strict" in str(err)
    assert isinstance(err, RuntimeError)
