"""Read-time redaction of sensitive fields (N18) — re-export of :mod:`satay.redaction`.

The redactor itself moved to the core module :mod:`satay.redaction` when write-time
redaction landed (ADR-0026 decision 4, ADR-0028): the write path is the journal store,
which is core (A3) and must not import the control package (A7/A8). This module stays as
the read-path spelling — :class:`satay.control.api.ReadAPI` applies the redactor as the
final transform to *every* read response, so there is no path that returns a run's data
unredacted — and as a stable import for existing callers.

Read-time redaction is unchanged and remains the default: with the write-time mode off
(the local case), the raw value is still in ``satay.db`` and the redactor protects the
API response only.
"""

from __future__ import annotations

from satay.redaction import (
    DEFAULT_REDACTION_PATTERNS,
    REDACTED,
    VALUE_REF_FIELDS,
    Redactor,
)

__all__ = ["DEFAULT_REDACTION_PATTERNS", "REDACTED", "VALUE_REF_FIELDS", "Redactor"]
