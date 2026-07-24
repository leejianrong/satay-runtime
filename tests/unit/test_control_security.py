"""Unit tests: the ADR-0014 local-surface security policy and loopback guard."""

from __future__ import annotations

import pytest

from satay.control.security import (
    AuthError,
    NonLoopbackBindError,
    SecurityPolicy,
    ensure_loopback_bind,
    generate_token,
    is_loopback_host,
)


def test_generate_token_is_unpredictable() -> None:
    assert generate_token() != generate_token()
    assert len(generate_token()) >= 32


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "[::1]:8000", "127.0.0.1:9"])
def test_loopback_hosts_are_recognised(host: str) -> None:
    assert is_loopback_host(host)


@pytest.mark.parametrize("host", ["0.0.0.0", "10.0.0.5", "evil.example.com"])
def test_non_loopback_hosts_are_rejected(host: str) -> None:
    assert not is_loopback_host(host)


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "example.com"])
def test_ensure_loopback_bind_refuses_non_loopback(host: str) -> None:
    with pytest.raises(NonLoopbackBindError):
        ensure_loopback_bind(host)


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_ensure_loopback_bind_accepts_loopback(host: str) -> None:
    ensure_loopback_bind(host)  # does not raise


def test_missing_or_invalid_token_is_rejected() -> None:
    policy = SecurityPolicy(token="good")
    with pytest.raises(AuthError) as none_exc:
        policy.check(token=None, host="localhost", origin=None)
    assert none_exc.value.status == 401
    with pytest.raises(AuthError) as bad_exc:
        policy.check(token="wrong", host="localhost", origin=None)
    assert bad_exc.value.status == 401


def test_valid_token_and_loopback_host_pass() -> None:
    policy = SecurityPolicy(token="good")
    policy.check(token="good", host="127.0.0.1:8000", origin=None)  # no raise


def test_disallowed_host_is_rejected() -> None:
    policy = SecurityPolicy(token="good")
    with pytest.raises(AuthError) as exc:
        policy.check(token="good", host="attacker.example.com", origin=None)
    assert exc.value.status == 403


def test_cross_origin_is_rejected_but_loopback_origin_allowed() -> None:
    policy = SecurityPolicy(token="good")
    policy.check(token="good", host="localhost", origin="http://localhost:5173")  # no raise
    with pytest.raises(AuthError) as exc:
        policy.check(token="good", host="localhost", origin="http://evil.example.com")
    assert exc.value.status == 403
