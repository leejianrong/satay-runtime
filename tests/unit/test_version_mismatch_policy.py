"""Unit tests for version-mismatch-policy parsing (N17, ADR-0010/0023).

The policy is deliberately separate from ``effect_safety``, which used to govern it.
Its default is ``warn`` — the behaviour the check already had while it read
``effect_safety``'s ``warn`` default, preserved by ADR-0023.
"""

from __future__ import annotations

import pytest

from satay.config import VersionMismatchPolicy, resolve_version_mismatch


def test_parse_defaults_to_warn() -> None:
    """Pins the preserved default: ADR-0023 made the coupling explicit, not stricter."""
    assert VersionMismatchPolicy.parse(None) is VersionMismatchPolicy.WARN


def test_parse_accepts_the_three_modes_case_insensitively() -> None:
    assert VersionMismatchPolicy.parse("off") is VersionMismatchPolicy.OFF
    assert VersionMismatchPolicy.parse("WARN") is VersionMismatchPolicy.WARN
    assert VersionMismatchPolicy.parse(" Strict ") is VersionMismatchPolicy.STRICT
    assert VersionMismatchPolicy.parse(VersionMismatchPolicy.OFF) is VersionMismatchPolicy.OFF


def test_parse_rejects_unknown_values() -> None:
    with pytest.raises(ValueError, match="unknown version_mismatch mode"):
        VersionMismatchPolicy.parse("paranoid")


def test_resolve_prefers_override_then_env_then_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SATAY_VERSION_MISMATCH", raising=False)
    assert resolve_version_mismatch() is VersionMismatchPolicy.WARN
    monkeypatch.setenv("SATAY_VERSION_MISMATCH", "strict")
    assert resolve_version_mismatch() is VersionMismatchPolicy.STRICT
    # An explicit override wins over the env var.
    assert resolve_version_mismatch("off") is VersionMismatchPolicy.OFF


def test_the_three_policies_read_separate_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    """``SATAY_EFFECT_SAFETY`` must not move the version-mismatch policy, or vice versa."""
    from satay.config import (
        EffectSafety,
        NondeterminismPolicy,
        resolve_effect_safety,
        resolve_nondeterminism,
    )

    for var in ("SATAY_EFFECT_SAFETY", "SATAY_NONDETERMINISM", "SATAY_VERSION_MISMATCH"):
        monkeypatch.delenv(var, raising=False)

    monkeypatch.setenv("SATAY_EFFECT_SAFETY", "off")
    assert resolve_effect_safety() is EffectSafety.OFF
    assert resolve_version_mismatch() is VersionMismatchPolicy.WARN
    assert resolve_nondeterminism() is NondeterminismPolicy.STRICT

    monkeypatch.delenv("SATAY_EFFECT_SAFETY")
    monkeypatch.setenv("SATAY_VERSION_MISMATCH", "off")
    assert resolve_version_mismatch() is VersionMismatchPolicy.OFF
    assert resolve_effect_safety() is EffectSafety.WARN
    assert resolve_nondeterminism() is NondeterminismPolicy.STRICT


def test_it_is_a_distinct_type_from_the_other_two_policies() -> None:
    """Same vocabulary, different types — a swapped argument is a ``mypy`` error, and
    the members do not compare equal by identity across the enums (ADR-0022/0023)."""
    from satay.config import EffectSafety, NondeterminismPolicy

    assert VersionMismatchPolicy.STRICT is not EffectSafety.STRICT
    assert VersionMismatchPolicy.STRICT is not NondeterminismPolicy.STRICT
