"""Unit tests: the version-mismatch policy on resume (N17, ADR-0010/0023).

Pure over the policy functions — no store. Shares the ``off``/``warn``/``strict`` shape
with nondeterminism (V2) and effect-safety enforcement, but is its own setting since
ADR-0023: ``strict`` raises, ``warn`` logs and continues, ``off`` is silent, and a
matching version is always a no-op.
"""

from __future__ import annotations

import logging

import pytest

from satay.config import VersionMismatchPolicy
from satay.versioning import VersionMismatchError, check_resume_version, is_version_mismatch


def test_is_version_mismatch_detects_a_difference() -> None:
    assert is_version_mismatch("git:aaa", "git:bbb") is True
    assert is_version_mismatch("git:aaa", "git:aaa") is False


def test_strict_rejects_the_resume_on_mismatch() -> None:
    with pytest.raises(VersionMismatchError) as excinfo:
        check_resume_version("git:old", "git:new", VersionMismatchPolicy.STRICT)
    # The error carries both versions so the developer sees what changed.
    assert excinfo.value.stamped == "git:old"
    assert excinfo.value.current == "git:new"


def test_warn_allows_the_resume_but_logs(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="satay"):
        check_resume_version("git:old", "git:new", VersionMismatchPolicy.WARN)  # does not raise
    assert "mismatch" in caplog.text.lower()


def test_off_is_silent_on_mismatch() -> None:
    # No raise, no log requirement.
    check_resume_version("git:old", "git:new", VersionMismatchPolicy.OFF)


def test_matching_version_is_a_noop_in_every_mode() -> None:
    for mode in VersionMismatchPolicy:
        check_resume_version("git:same", "git:same", mode)  # never raises
