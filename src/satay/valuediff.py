"""Structural diff of two recorded values, as paths rather than as values.

Answers the question the compare view could not: not *that* two aligned durable calls
differ, but **where**. Studio has always computed four booleans client-side
(input/output/attempts/duration changed), which tells a reader a prompt changed without
telling them which field of it changed — and for an agent developer comparing a run to
its fork, that *is* the question (ADR-0025: the debugger is the wedge).

**Why this module is core, not `satay.control`.** Both the HTTP compare view (A7/A8) and
the core `satay.diff` entry point (A1) need the same algorithm, and the shared thing
belongs at the bottom where both can reach down to it. This is the arrangement
[ADR-0029](../../docs/adr/0029-write-time-redaction.md) chose for the redactor, for the
same reason and after the same mistake.

**Paths, never values.** The diff is computed *before* read-time redaction, so it is
correct even where the response masks the values it describes — two different secrets are
correctly reported as differing, where a diff computed after redaction would see
``***REDACTED***`` on both sides and report them identical. Emitting only paths is what
makes that safe: the redactor preserves mapping *keys* and masks their values, so a path
built from those keys discloses nothing the response did not already carry.

The one case that cannot be rescued: with write-time redaction on
(ADR-0029), the journal itself holds the sentinel and the cleartext is gone at every
layer. That is reported as ``redacted``, never silently as "identical" — an honest "cannot
compare" beats a confident wrong answer.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

from satay.redaction import REDACTED

#: Cap on reported paths. Recorded values are unbounded — payloads spill to blob files
#: past 256 KiB (ADR-0004) and Studio re-polls compare every couple of seconds — so a
#: diff of two large, wholly different structures must not walk or emit without limit.
MAX_PATHS: Final = 50

#: Cap on recursion depth. Below it, a node is reported as one differing path rather than
#: descended into, which keeps a deeply nested payload from producing a path per leaf.
MAX_DEPTH: Final = 8

#: The whole value, when the difference is not localisable to any field inside it (a
#: scalar, or two sides of different shapes). jq's spelling, so the path vocabulary is one
#: a reader already knows rather than a third one invented here.
ROOT: Final = "."


def _is_mapping(value: Any) -> bool:
    return isinstance(value, Mapping)


def _is_sequence(value: Any) -> bool:
    """A list-like, excluding the string types that are ``Sequence`` but not containers."""
    return isinstance(value, Sequence) and not isinstance(value, str | bytes)


def _join(prefix: str, segment: str) -> str:
    if prefix == ROOT:
        return segment if segment.startswith("[") else f".{segment}"
    return f"{prefix}{segment}" if segment.startswith("[") else f"{prefix}.{segment}"


class _Walk:
    """Accumulates differing paths, and whether the walk hit a cap or a masked value."""

    def __init__(self) -> None:
        self.paths: list[str] = []
        self.redacted = False
        self.truncated = False

    def add(self, path: str) -> None:
        if len(self.paths) >= MAX_PATHS:
            self.truncated = True
            return
        self.paths.append(path)

    def full(self) -> bool:
        return self.truncated and len(self.paths) >= MAX_PATHS


def _walk(a: Any, b: Any, path: str, depth: int, out: _Walk) -> None:
    if out.full():
        return

    # A masked leaf on either side means equality is unknowable, not that the values
    # match. Only write-time redaction can put the sentinel here, since this runs before
    # the read-time pass.
    if a == REDACTED or b == REDACTED:
        if a != b:
            out.add(path)
        else:
            out.redacted = True
        return

    if depth >= MAX_DEPTH:
        if a != b:
            out.truncated = True
            out.add(path)
        return

    if _is_mapping(a) and _is_mapping(b):
        for key in a:
            if key not in b:
                out.add(_join(path, str(key)))
            else:
                _walk(a[key], b[key], _join(path, str(key)), depth + 1, out)
        for key in b:
            if key not in a:
                out.add(_join(path, str(key)))
        return

    if _is_sequence(a) and _is_sequence(b):
        if len(a) != len(b):
            # A length change is a property of the node, not of any one element: pairing
            # index-by-index after an insertion would report every later element as
            # changed, which is noise rather than information.
            out.add(path)
            return
        for index, (left, right) in enumerate(zip(a, b, strict=True)):
            _walk(left, right, _join(path, f"[{index}]"), depth + 1, out)
        return

    if a != b:
        out.add(path)


def diff_values(a: Any, b: Any) -> dict[str, Any]:
    """Where two recorded values differ, as jq-style paths.

    Returns ``{"changed", "paths", "redacted", "truncated"}``. ``paths`` holds the
    differing locations — ``.style``, ``[1].topic``, or :data:`ROOT` when the difference
    is not localisable (a scalar, or two sides of different shapes). For a call's
    arguments the top-level index **is** the positional argument index, because keyword
    arguments are never journaled.

    ``redacted`` says a compared leaf was masked in the journal itself, so its equality is
    unknown; ``truncated`` says a cap was hit and the paths are a prefix of the truth.
    Both are reported rather than resolved: this function never guesses.
    """
    out = _Walk()
    _walk(a, b, ROOT, 0, out)
    return {
        "changed": bool(out.paths),
        "paths": out.paths,
        "redacted": out.redacted,
        "truncated": out.truncated,
    }
