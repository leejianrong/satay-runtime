"""Unit tests: redaction field-pattern matching flags configured field names (N18)."""

from __future__ import annotations

from satay.control.redaction import DEFAULT_REDACTION_PATTERNS, REDACTED, Redactor


def test_default_patterns_flag_sensitive_field_names() -> None:
    redactor = Redactor()
    for field in ("password", "API_KEY", "AccessKey", "session_token", "authorization"):
        assert redactor.matches(field), field


def test_structural_contract_keys_are_not_flagged() -> None:
    """Contract keys the timeline/tree emit must never be caught by the default set."""
    redactor = Redactor()
    for field in ("key", "code_version", "event_id", "identity", "run_id", "ordinal"):
        assert not redactor.matches(field), field


def test_redact_masks_matching_values_and_recurses() -> None:
    redactor = Redactor()
    out = redactor.redact(
        {
            "run_id": "r1",
            "api_key": "secret-123",
            "nested": {"password": "hunter2", "label": "keep"},
            "items": [{"token": "abc"}, {"ok": 1}],
        }
    )
    assert out["run_id"] == "r1"
    assert out["api_key"] == REDACTED
    assert out["nested"]["password"] == REDACTED
    assert out["nested"]["label"] == "keep"
    assert out["items"][0]["token"] == REDACTED
    assert out["items"][1]["ok"] == 1


def test_redact_does_not_mutate_input() -> None:
    redactor = Redactor()
    original = {"secret": "s", "ok": "v"}
    redactor.redact(original)
    assert original == {"secret": "s", "ok": "v"}


def test_custom_patterns_override_defaults() -> None:
    redactor = Redactor(patterns=["ssn"])
    assert redactor.matches("ssn")
    assert not redactor.matches("password")  # not in the custom set


def test_default_pattern_set_is_frozen() -> None:
    assert isinstance(DEFAULT_REDACTION_PATTERNS, frozenset)
    assert "secret" in DEFAULT_REDACTION_PATTERNS
