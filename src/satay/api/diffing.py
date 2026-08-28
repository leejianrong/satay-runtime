"""``satay.diff``: where two runs' recorded calls differ, call by call and field by field.

The debugger wedge in ADR-0025 is fork, replay, and **call-by-call compare**, locally. The
first two were reachable from Python; the third was not. Studio has always shown a
two-run compare, but its diff was four booleans computed in the browser — enough to say a
prompt changed, not which field of it changed. And from a script there was nothing at all:
``examples/fork_and_compare_demo.py`` hand-rolls its own output comparison because no API
offered one.

``satay.diff`` closes that, on the same terms as :func:`satay.inspect` (ADR-0033): reads
only, redacted by default, values decoded but untyped. The structural comparison itself
lives in :mod:`satay.valuediff`, in the core, because the HTTP compare view needs the same
algorithm and shared logic belongs where both can reach down to it.

Paths use jq's spelling — ``.style``, ``[1].topic``, ``.`` for the whole value — so the
vocabulary is one a reader already knows. For a call's arguments the top-level index *is*
the positional argument index, since keyword arguments are never journaled.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from satay.api.inspection import RecordedCall, _recorded_args
from satay.journal.events import CallStatus

if TYPE_CHECKING:
    from satay.journal import Store
    from satay.redaction import Redactor


@dataclass(frozen=True, slots=True)
class ValueDiff:
    """Where one field's two recorded values differ."""

    changed: bool
    paths: tuple[str, ...] = ()
    """The differing locations, jq-style. ``(".",)`` means the difference is not
    localisable to any field inside the value — a scalar, or two sides of different
    shapes."""

    redacted: bool = False
    """A compared leaf was masked **in the journal itself** (write-time redaction,
    ADR-0029), so its equality is unknown rather than established. Reported instead of
    being silently counted as identical: read-time redaction cannot cause this, because the
    comparison runs before it."""

    truncated: bool = False
    """A cap was hit, so ``paths`` is a prefix of the truth rather than all of it.
    Recorded values are unbounded — payloads spill to blob files past 256 KiB."""


@dataclass(frozen=True, slots=True)
class CallDiff:
    """One durable-call identity, as it appears in each of the two runs."""

    identity: str
    task_name: str | None
    a: RecordedCall | None
    b: RecordedCall | None

    aligned: bool
    """Present on both sides. A call only one run made cannot be compared, only noted."""

    changed: bool
    """Absent on one side, or differing arguments, output, or attempts.

    Timing is excluded on purpose: duration varies between runs for reasons that are not a
    divergence, so counting it would mark every call changed."""

    args: ValueDiff | None = None
    output: ValueDiff | None = None
    """``None`` on an unaligned call — there is no second value to compare against."""

    attempts_changed: bool = False
    duration_changed: bool = False


@dataclass(frozen=True, slots=True)
class RunDiff:
    """Two runs aligned by durable-call identity."""

    a_run_id: str
    b_run_id: str
    calls: tuple[CallDiff, ...] = field(default_factory=tuple)
    """One entry per identity present in either run, ordered by identity.

    Identity order, not schedule order: the two runs may have scheduled the same call at
    different points, so neither run's sequence can order a shared list. This is the same
    ordering the HTTP compare view uses, and it is why :func:`satay.inspect` — which has
    only one run to follow — keeps schedule order instead."""

    @property
    def changed(self) -> tuple[CallDiff, ...]:
        """Only the calls that differ. The usual thing a caller wants to look at."""
        return tuple(call for call in self.calls if call.changed)


def _value_diff(raw: dict[str, Any] | None) -> ValueDiff | None:
    if raw is None:
        return None
    return ValueDiff(
        changed=bool(raw["changed"]),
        paths=tuple(raw["paths"]),
        redacted=bool(raw["redacted"]),
        truncated=bool(raw["truncated"]),
    )


def _side(call: dict[str, Any] | None) -> RecordedCall | None:
    if call is None:
        return None
    return RecordedCall(
        identity=call.get("identity", ""),
        task_name=call["task_name"],
        args=_recorded_args(call.get("input")),
        output=call.get("output"),
        status=CallStatus(call["status"]),
        attempts=call["attempts"],
        duration_seconds=call["duration_seconds"],
        ordinal=call.get("ordinal"),
        key=call.get("key"),
        map_group=call.get("map_group"),
        child_run_id=call.get("child_run_id"),
        first_seq=call.get("first_seq", 0),
    )


async def diff(
    run_id: str,
    other_run_id: str,
    *,
    store: Store | None = None,
    redactor: Redactor | None = None,
) -> RunDiff:
    """Compare two runs call by call, and report where their values differ.

    The natural companion to :func:`satay.fork`: fork a run at a bad call, drive the fork,
    then diff the two to see exactly what the change did. Neither run is modified, and
    nothing re-executes.

    Redacted by default, with the same override as :func:`satay.inspect`. The *paths* are
    computed before redaction and so stay correct even where the values they point at come
    back masked — but a value masked in the journal itself sets ``ValueDiff.redacted``,
    because equality is then genuinely unknown. Raises :class:`LookupError` if either run
    id is unknown.

    Comparison is Python equality over decoded values, so ``True``/``1`` and ``2``/``2.0``
    count as equal and mapping key order is ignored — Studio's Compare view (ADR-0034)
    reads this same structural diff rather than keeping a second, JSON-string-equality
    implementation of its own.
    """
    from satay.api.primitives import _default_store
    from satay.control import views
    from satay.redaction import Redactor

    resolved = store if store is not None else _default_store()
    view = await views.compare(resolved, run_id, other_run_id)
    view = (redactor or Redactor()).redact(view)

    calls = tuple(
        CallDiff(
            identity=row["identity"],
            task_name=row.get("task_name"),
            a=_side(row.get("a")),
            b=_side(row.get("b")),
            aligned=bool(row["aligned"]),
            changed=bool(row["diff"]["changed"]),
            args=_value_diff(row["diff"]["input"]),
            output=_value_diff(row["diff"]["output"]),
            attempts_changed=bool(row["diff"]["attempts"]),
            duration_changed=bool(row["diff"]["duration_seconds"]),
        )
        for row in view["rows"]
    )
    return RunDiff(a_run_id=view["a"]["run_id"], b_run_id=view["b"]["run_id"], calls=calls)
