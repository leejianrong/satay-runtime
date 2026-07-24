"""Read-time redaction of sensitive fields (N18).

Sensitive values are stored as ordinary journal data (the runtime never treats them
specially on write) and stripped on the way out by the :class:`Redactor`, a pure
transform over the JSON-compatible structure a read view returns. Redaction is keyed
on **field names**: any mapping key whose lower-cased form contains a configured
pattern has its value replaced with :data:`REDACTED`, recursing through nested
mappings and lists. Because it is applied as the final transform to *every* read
response (see :class:`satay.control.api.ReadAPI`), there is no path that returns a
run's data unredacted.

This is deliberately a core, pure-Python module (no FastAPI): the boundary constraint
is that ``import satay.control`` must not pull the studio stack (ADR-0013).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

#: The placeholder substituted for a redacted value.
REDACTED = "***REDACTED***"

#: Default field-name patterns (matched case-insensitively as substrings of a key).
#: Deliberately specific so structural contract keys — ``key`` (a map item key),
#: ``code_version``, ``event_id``, ``identity`` — are never caught. Callers may pass
#: their own set to :class:`Redactor`.
DEFAULT_REDACTION_PATTERNS: frozenset[str] = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "api_key",
        "apikey",
        "access_key",
        "accesskey",
        "private_key",
        "credential",
        "authorization",
        "session_token",
    }
)


class Redactor:
    """Redacts values whose field name matches a configured pattern (N18)."""

    def __init__(self, patterns: Iterable[str] | None = None) -> None:
        source = DEFAULT_REDACTION_PATTERNS if patterns is None else patterns
        #: Lower-cased for case-insensitive substring matching.
        self._patterns: tuple[str, ...] = tuple(p.lower() for p in source)

    def matches(self, field_name: str) -> bool:
        """Whether ``field_name`` is a sensitive field (case-insensitive substring)."""
        lowered = field_name.lower()
        return any(pattern in lowered for pattern in self._patterns)

    def redact(self, value: Any) -> Any:
        """Return a redacted deep copy of a JSON-compatible ``value``.

        Mappings are rebuilt with matching keys' values replaced by :data:`REDACTED`
        (nested structure under a matched key is not walked — the whole value is
        masked); non-matching values recurse. Lists/tuples recurse element-wise.
        Scalars pass through unchanged. The input is never mutated.
        """
        if isinstance(value, Mapping):
            out: dict[str, Any] = {}
            for key, item in value.items():
                if isinstance(key, str) and self.matches(key):
                    out[key] = REDACTED
                else:
                    out[key] = self.redact(item)
            return out
        if isinstance(value, str | bytes):
            return value
        if isinstance(value, Sequence):
            return [self.redact(item) for item in value]
        return value


__all__ = ["DEFAULT_REDACTION_PATTERNS", "REDACTED", "Redactor"]
