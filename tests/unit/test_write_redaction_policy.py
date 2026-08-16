"""Unit tests: the write-redaction mode knob and slot-scoped redaction (ADR-0028).

The knob resolves like every other project setting (override → env var → default), and
the slot-scoped transform is where the replay-identity guarantee lives: structural fields
survive *any* pattern set, including one aimed straight at them.
"""

from __future__ import annotations

import pytest

from satay.config import (
    WRITE_REDACTION_ENV_VAR,
    EffectSafety,
    NondeterminismPolicy,
    WriteRedaction,
    resolve_write_redaction,
)
from satay.redaction import REDACTED, VALUE_REF_FIELDS, Redactor


def test_default_is_off_so_read_time_stays_the_local_default() -> None:
    assert resolve_write_redaction() is WriteRedaction.OFF
    assert WriteRedaction.parse(None) is WriteRedaction.OFF
    assert not WriteRedaction.OFF.enabled


def test_explicit_override_wins_over_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(WRITE_REDACTION_ENV_VAR, "off")
    assert resolve_write_redaction("on") is WriteRedaction.ON
    assert resolve_write_redaction(WriteRedaction.ON).enabled


def test_env_var_turns_it_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(WRITE_REDACTION_ENV_VAR, "  ON  ")
    assert resolve_write_redaction() is WriteRedaction.ON


def test_unknown_mode_names_the_setting_and_valid_values() -> None:
    with pytest.raises(ValueError, match="write_redaction"):
        WriteRedaction.parse("strict")


def test_it_is_a_distinct_enum_from_the_three_policies() -> None:
    """A mix-up must be a mypy error, not a silently-accepted argument (cf. ADR-0022)."""
    assert WriteRedaction.OFF is not EffectSafety.OFF
    assert WriteRedaction.OFF is not NondeterminismPolicy.OFF
    assert [m.value for m in WriteRedaction] == ["off", "on"]


def test_other_policy_env_vars_do_not_turn_it_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SATAY_EFFECT_SAFETY", "strict")
    monkeypatch.setenv("SATAY_NONDETERMINISM", "strict")
    assert resolve_write_redaction() is WriteRedaction.OFF


def test_value_slots_are_the_ref_indirection_fields() -> None:
    assert set(VALUE_REF_FIELDS) == {"input_ref", "output_ref", "event_ref"}


def test_slot_scoped_redaction_masks_values_and_keeps_structure() -> None:
    redactor = Redactor()
    out = redactor.redact_value_slots(
        {
            "task_name": "charge",
            "ordinal": 3,
            "input_ref": [{"api_key": "sk-live", "amount": 10}],
            "output_ref": {"session_token": "abc", "ok": True},
        }
    )
    assert out["task_name"] == "charge"
    assert out["ordinal"] == 3
    assert out["input_ref"] == [{"api_key": REDACTED, "amount": 10}]
    assert out["output_ref"] == {"session_token": REDACTED, "ok": True}


def test_slot_scoping_protects_identity_from_a_hostile_pattern_set() -> None:
    """The replay-identity guarantee: no pattern set can reach a structural field."""
    hostile = Redactor(patterns=["key", "name", "ordinal", "identity", "run_id"])
    payload = {
        "task_name": "draft",
        "key": "item-7",
        "identity": "draft#item-7",
        "run_id": "r1",
        "input_ref": [{"api_key": "sk-live"}],
    }
    out = hostile.redact_value_slots(payload)

    assert out["task_name"] == "draft"
    assert out["key"] == "item-7"  # the fan-out identity survives
    assert out["identity"] == "draft#item-7"
    assert out["run_id"] == "r1"
    assert out["input_ref"] == [{"api_key": REDACTED}]  # the value does not
    # A whole-payload redact() is what slot scoping exists to avoid.
    assert hostile.redact(payload)["key"] == REDACTED


def test_slot_scoped_redaction_does_not_mutate_and_is_idempotent() -> None:
    redactor = Redactor()
    original = {"input_ref": {"secret": "s"}}
    once = redactor.redact_value_slots(original)
    assert original == {"input_ref": {"secret": "s"}}
    assert redactor.redact_value_slots(once) == once  # redacting a placeholder is a no-op


def test_non_mapping_payloads_pass_through() -> None:
    redactor = Redactor()
    assert redactor.redact_value_slots(None) is None
    assert redactor.redact_value_slots(["a", "b"]) == ["a", "b"]
