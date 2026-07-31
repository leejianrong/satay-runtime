"""Unit tests for nondeterminism-policy parsing (N9, ADR-0003/0022).

The policy is deliberately separate from ``effect_safety`` and defaults to ``strict``,
so a replay divergence raises unless something opted out. See ADR-0022.
"""

from __future__ import annotations

import pytest

from satay.config import NondeterminismPolicy, resolve_nondeterminism


def test_parse_defaults_to_strict() -> None:
    assert NondeterminismPolicy.parse(None) is NondeterminismPolicy.STRICT


def test_parse_accepts_the_three_modes_case_insensitively() -> None:
    assert NondeterminismPolicy.parse("off") is NondeterminismPolicy.OFF
    assert NondeterminismPolicy.parse("WARN") is NondeterminismPolicy.WARN
    assert NondeterminismPolicy.parse(" Strict ") is NondeterminismPolicy.STRICT
    assert NondeterminismPolicy.parse(NondeterminismPolicy.OFF) is NondeterminismPolicy.OFF


def test_parse_rejects_unknown_values() -> None:
    with pytest.raises(ValueError, match="unknown nondeterminism mode"):
        NondeterminismPolicy.parse("paranoid")


def test_resolve_prefers_override_then_env_then_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SATAY_NONDETERMINISM", raising=False)
    assert resolve_nondeterminism() is NondeterminismPolicy.STRICT
    monkeypatch.setenv("SATAY_NONDETERMINISM", "warn")
    assert resolve_nondeterminism() is NondeterminismPolicy.WARN
    # An explicit override wins over the env var.
    assert resolve_nondeterminism("off") is NondeterminismPolicy.OFF


def test_the_two_policies_read_separate_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    """``SATAY_EFFECT_SAFETY`` must not move the nondeterminism policy, or vice versa."""
    from satay.config import EffectSafety, resolve_effect_safety

    monkeypatch.delenv("SATAY_NONDETERMINISM", raising=False)
    monkeypatch.setenv("SATAY_EFFECT_SAFETY", "off")
    assert resolve_effect_safety() is EffectSafety.OFF
    assert resolve_nondeterminism() is NondeterminismPolicy.STRICT

    monkeypatch.delenv("SATAY_EFFECT_SAFETY", raising=False)
    monkeypatch.setenv("SATAY_NONDETERMINISM", "off")
    assert resolve_nondeterminism() is NondeterminismPolicy.OFF
    assert resolve_effect_safety() is EffectSafety.WARN
