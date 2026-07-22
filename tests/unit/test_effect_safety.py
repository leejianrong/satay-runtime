"""Unit tests for effect-safety mode parsing (A10.2, ADR-0006)."""

from __future__ import annotations

import pytest

from satay.config import EffectSafety, resolve_effect_safety


def test_parse_defaults_to_warn_in_dev() -> None:
    assert EffectSafety.parse(None) is EffectSafety.WARN


def test_parse_accepts_the_three_modes_case_insensitively() -> None:
    assert EffectSafety.parse("off") is EffectSafety.OFF
    assert EffectSafety.parse("WARN") is EffectSafety.WARN
    assert EffectSafety.parse(" Strict ") is EffectSafety.STRICT
    assert EffectSafety.parse(EffectSafety.STRICT) is EffectSafety.STRICT


def test_parse_rejects_unknown_values() -> None:
    with pytest.raises(ValueError, match="unknown effect_safety mode"):
        EffectSafety.parse("paranoid")


def test_resolve_prefers_override_then_env_then_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SATAY_EFFECT_SAFETY", raising=False)
    assert resolve_effect_safety() is EffectSafety.WARN
    monkeypatch.setenv("SATAY_EFFECT_SAFETY", "strict")
    assert resolve_effect_safety() is EffectSafety.STRICT
    # An explicit override wins over the env var.
    assert resolve_effect_safety("off") is EffectSafety.OFF
