"""Integration tests for the code-version stamper fallback order (N17, stamp-only)."""

from __future__ import annotations

import pytest

from satay import versioning


async def _dummy() -> None:  # pragma: no cover - only used as a hash target
    return None


def test_git_commit_wins_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(versioning, "_git_commit", lambda: "abc123")
    assert versioning.stamp_code_version() == "git:abc123"


def test_dev_string_used_when_no_git(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(versioning, "_git_commit", lambda: None)
    monkeypatch.delenv(versioning.DEV_VERSION_ENV_VAR, raising=False)
    assert versioning.stamp_code_version(dev_string="v1.2.3") == "dev:v1.2.3"


def test_dev_string_from_env_when_no_git(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(versioning, "_git_commit", lambda: None)
    monkeypatch.setenv(versioning.DEV_VERSION_ENV_VAR, "envver")
    assert versioning.stamp_code_version() == "dev:envver"


def test_source_hash_is_last_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(versioning, "_git_commit", lambda: None)
    monkeypatch.delenv(versioning.DEV_VERSION_ENV_VAR, raising=False)
    stamped = versioning.stamp_code_version(source_targets=[_dummy])
    assert stamped.startswith("src:")
    # Deterministic: the same source hashes identically.
    assert stamped == versioning.stamp_code_version(source_targets=[_dummy])
