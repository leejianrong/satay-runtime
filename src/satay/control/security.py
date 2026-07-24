"""Local-surface security policy (ADR-0014): token, Origin/Host allow-list, loopback.

A browser-reachable localhost API is not safe by default: another tab can POST to a
predictable ``127.0.0.1:<port>`` endpoint, and DNS-rebinding can bypass same-origin to
read runs back (ADR-0014). This module is the cheap, proportionate guard:

- a **per-session token** every request must present (via the ``X-Satay-Token``
  header), generated at server start;
- an **``Origin``/``Host`` allow-list** — a cross-origin ``Origin`` or an unexpected
  ``Host`` is rejected (DNS-rebinding defence);
- a **loopback-bind check** — the server refuses to bind a non-loopback address.

Pure Python (stdlib ``secrets``/``ipaddress`` only): the check runs inside the HTTP
surface but must not drag the studio stack into ``import satay.control``. The FastAPI
layer (``satay.control.server``) calls :meth:`SecurityPolicy.check` per request.
"""

from __future__ import annotations

import ipaddress
import secrets
from dataclasses import dataclass

#: The header carrying the per-session token on every request (ADR-0014).
TOKEN_HEADER = "x-satay-token"

#: Loopback host names that are always allowed (in addition to loopback IPs).
_LOOPBACK_NAMES = frozenset({"localhost", "localhost.localdomain"})


def generate_token() -> str:
    """Generate a per-session bearer token (URL-safe, ~256 bits)."""
    return secrets.token_urlsafe(32)


def _hostname_only(value: str) -> str:
    """Strip a port (and brackets) from a ``Host``/``Origin`` authority."""
    host = value.strip()
    if "://" in host:
        host = host.split("://", 1)[1]
    if host.startswith("[") and "]" in host:  # bracketed IPv6, e.g. [::1]:8000
        return host[1 : host.index("]")]
    if host.count(":") == 1:  # host:port
        host = host.rsplit(":", 1)[0]
    return host


def is_loopback_host(value: str) -> bool:
    """Whether a bare host / ``Host`` header refers to loopback."""
    host = _hostname_only(value)
    if host.lower() in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def ensure_loopback_bind(host: str) -> None:
    """Raise :class:`NonLoopbackBindError` unless ``host`` is a loopback address.

    ``satay dev`` binds loopback only (ADR-0014); a non-loopback bind (``0.0.0.0``, a
    LAN IP) is refused because the local-surface guard is not real network auth.
    """
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if host.lower() in _LOOPBACK_NAMES:
            return
        raise NonLoopbackBindError(host) from None
    if not address.is_loopback:
        raise NonLoopbackBindError(host)


class NonLoopbackBindError(ValueError):
    """Raised when the server is asked to bind a non-loopback address (ADR-0014)."""

    def __init__(self, host: str) -> None:
        super().__init__(
            f"refusing to bind non-loopback address {host!r}; the local-surface guard "
            f"is not network authentication (ADR-0014)"
        )
        self.host = host


class AuthError(Exception):
    """A request rejected by the security policy; ``status`` maps to the HTTP code."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass(frozen=True, slots=True)
class SecurityPolicy:
    """Enforces the ADR-0014 guard on an incoming request.

    ``token`` is the per-session secret. ``allowed_origins`` is the exact set of
    acceptable ``Origin`` values (empty means "reject any cross-origin ``Origin``",
    while a *missing* ``Origin`` — a same-origin/non-browser request — is allowed).
    ``Host`` must resolve to loopback.
    """

    token: str
    allowed_origins: frozenset[str] = frozenset()

    def check(self, *, token: str | None, host: str | None, origin: str | None) -> None:
        """Raise :class:`AuthError` if the request violates the guard; else return."""
        if not token or not secrets.compare_digest(token, self.token):
            raise AuthError(401, "missing or invalid session token")
        if host is not None and not is_loopback_host(host):
            raise AuthError(403, f"disallowed Host {host!r}")
        if origin is not None:
            origin_host = _hostname_only(origin)
            if origin not in self.allowed_origins and not is_loopback_host(origin_host):
                raise AuthError(403, f"disallowed Origin {origin!r}")


__all__ = [
    "TOKEN_HEADER",
    "AuthError",
    "NonLoopbackBindError",
    "SecurityPolicy",
    "ensure_loopback_bind",
    "generate_token",
    "is_loopback_host",
]
