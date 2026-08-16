"""Field-name redaction of sensitive values — read time and write time (N18, ADR-0029).

Redaction is keyed on **field names**: any mapping key whose lower-cased form contains a
configured pattern has its value replaced with :data:`REDACTED`, recursing through nested
mappings and lists. The same :class:`Redactor` serves two very different jobs:

- **Read time** (ADR-0009/0014, the default and the local case): the redactor is the
  final transform on every read response (:class:`satay.control.api.ReadAPI`), so nothing
  leaves the process unredacted. The raw value stays in ``satay.db``.
- **Write time** (ADR-0026 decision 4, ADR-0029, opt-in): the redactor runs on the
  recording path in :class:`satay.journal.store.SQLiteStore`, so the raw value never
  reaches the store at all — and the redacted form is therefore what the run resumes
  against.

**Why this module is core, not ``satay.control``.** The redactor started life next to the
read API. The write path is the journal store, which is core (A3) and must not import the
control plane (A7/A8, nominally the ``satay[studio]`` extra) — a core module reaching
"up" into the control package is the wrong direction even while both happen to be pure
Python. So the redactor lives here, next to :mod:`satay.config`, and
:mod:`satay.control.redaction` re-exports it for the read path and for existing callers.
Stdlib only: nothing here crosses the ADR-0013/0016 dependency boundary.

**Replay identity is out of scope by construction** (ADR-0029). Write-time redaction is
*slot-scoped*: it rewrites only the value-carrying ``*_ref`` slots of an event payload
(see :func:`is_value_slot`) and never touches the structural fields around them.
Durable-call identity is ``(task_name, ordinal)`` or ``(task_name, key)`` (ADR-0002), all
of which are structural, so no pattern set — not even one that deliberately matches
``key`` — can change what a replayed call matches.
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

#: The suffix that marks a payload field as a **value slot**. ADR-0004 puts every user
#: value behind ``*_ref`` indirection precisely so the envelope stays schema-stable, and
#: that naming convention is what slot-scoped write-time redaction keys on: a field whose
#: name ends in ``_ref`` holds data, everything else is runtime bookkeeping (``task_name``,
#: ``ordinal``, ``key``, ``identity``, ``code_version``, ids, timestamps) that replay
#: depends on and redaction must never rewrite (ADR-0029).
#:
#: The rule is the *suffix*, not a hand-maintained list, because the list is the part that
#: rots: a slice that adds an event type carrying a new ``*_ref`` gets redaction for free,
#: whereas a list has to be remembered, and the failure mode of forgetting is a secret in
#: the store — silent, and only visible to whoever ends up holding the journal.
VALUE_REF_SUFFIX = "_ref"

#: The value slots the journal carries today, for documentation and tests. Not the rule —
#: :func:`is_value_slot` is. ``payload_ref`` is the inbox's own column rather than a
#: payload field, and the store redacts it on that write path directly.
VALUE_REF_FIELDS: frozenset[str] = frozenset(
    {
        "input_ref",  # TaskScheduled / WorkflowCreated / ChildWorkflowScheduled arguments
        "output_ref",  # TaskCompleted / WorkflowCompleted results
        "event_ref",  # ExternalEventReceived delivered payload
        "source_input_ref",  # RunForked lineage: the input an override replaced (ADR-0028)
    }
)


def is_value_slot(field_name: str) -> bool:
    """Whether a payload field carries a user value rather than runtime structure.

    The write-time redaction boundary. Deliberately **excludes** the ``error`` payload of
    ``TaskAttemptFailed`` / ``TaskFailed``, which is a fixed ``{type, message, traceback}``
    of runtime-generated strings: no field name in it can match a pattern, so including it
    would protect nothing, while a custom pattern set that *did* match ``type`` would
    rewrite the ``error_type`` a collect-mode workflow branches on — manufacturing exactly
    the first-pass-versus-replay divergence ADR-0027 exists to prevent. A secret
    interpolated into an exception message is out of reach of field-name matching in both
    redaction modes, and is documented as such (ADR-0029).
    """
    return field_name.endswith(VALUE_REF_SUFFIX)


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

    def redact_value_slots(self, payload: Any) -> Any:
        """Redact only the value slots (see :func:`is_value_slot`) of an encoded payload.

        The write-time entry point (ADR-0029). Unlike :meth:`redact`, which walks a whole
        read view, this deliberately does **not** descend into the structural fields of a
        journal payload: ``task_name``, ``ordinal``, ``key``, ``identity``,
        ``code_version``, ``child_run_id``, ``error`` and their kin are handed back
        byte-identical whatever the pattern set says, so a redacted journal replays exactly
        like an unredacted one. Non-mapping payloads pass through unchanged; the input is
        never mutated.
        """
        if not isinstance(payload, Mapping):
            return payload
        return {
            key: (self.redact(value) if is_value_slot(key) else value)
            for key, value in payload.items()
        }


__all__ = [
    "DEFAULT_REDACTION_PATTERNS",
    "REDACTED",
    "VALUE_REF_FIELDS",
    "VALUE_REF_SUFFIX",
    "Redactor",
    "is_value_slot",
]
