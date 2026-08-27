"""``satay.inspect``: read a run's recorded durable calls without forking (KAN-477).

One of the launch-blocker usability cards in
[ADR-0025](../../../docs/adr/0025-positioning-agents-first.md). Reading back what a
finished run actually recorded is the first thing anybody does with a durable journal,
and until now the public surface had no answer:

- ``await handle.result()`` returns the *workflow's* output and nothing about the calls
  inside it, and raises for a failed run rather than describing the failure.
- ``satay.fork`` reaches the per-call data, but it **writes** (a new run row plus a
  journal prefix) and then **re-executes** every call after the fork point, and it needs
  the workflow still registered in this process. Paying a write and a re-drive to answer
  a read is the wrong shape.
- ``store.read_events`` works, but it hands back raw events and leaves the caller to
  group them by durable-call identity by hand — which the repo's own
  ``examples/fork_and_compare_demo.py`` does, across six internal imports.

Two design constraints are worth stating, because both make an obvious-looking API
wrong:

**Redaction.** ADR-0009 forces the ``Redactor`` on every read, and
:class:`satay.control.api.ReadAPI` is the only reason no HTTP read path returns
unredacted data. A Python read API that emits the same ``*_ref`` value slots to the same
kind of consumer belongs on the same side of that line, so this applies the redactor by
default and takes an override exactly as ``ReadAPI`` does. The two existing unredacted
Python paths are not precedent: ``handle.result()`` returns the caller's own workflow
value in-process, and ``satay runs show`` prints no value slots at all.

**No typed rehydration.** ADR-0005 rehydration (``rehydrate`` against a declared return
annotation) is deliberately *not* used here, for two independent reasons. It depends on
process state — it needs the task still registered, so the same run would read back as
different Python types depending on what the reader happened to import. And it is
incompatible with redaction: :meth:`~satay.redaction.Redactor.redact` is a JSON
deep-copy walk that silently redacts *nothing* when handed a dataclass or model
instance, so "typed objects" and "redacted" cannot both be true. Values therefore come
back decoded but untyped, the same as every other read view.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

    from satay.journal import Store
    from satay.journal.events import RunStatus
    from satay.redaction import Redactor


@dataclass(frozen=True, slots=True)
class RecordedCall:
    """One durable call as the journal recorded it.

    A "durable call" here means a task or a child workflow — the same vocabulary the read
    API and Studio use. Timers and event waits are *not* included: they occupy a separate
    identity namespace (``sleep#N`` / ``event#N``) that the read layer has never modelled
    as calls, and claiming otherwise would over-promise.
    """

    identity: str
    """The stable identity string, ``{task}:{ordinal}`` or ``{task}:key:{key}``
    (ADR-0002). The same token the read API's ``/tasks/{identity}`` path uses, so the two
    surfaces share one spelling."""

    task_name: str
    args: tuple[Any, ...]
    """The recorded **positional** arguments, in order. Keyword arguments are not
    journaled at all, so this is the whole recorded input and a single-value ``input``
    field would have been a lie for any multi-parameter task.

    Read-time redaction cannot mask a positional argument, here or in the HTTP read API:
    the ``Redactor`` matches *field names*, and a positional argument has none. A secret
    passed positionally to a task is in the journal in clear, and write-time redaction
    (ADR-0029) is the answer to that, not this read path."""

    output: Any
    """The recorded return value, or ``None`` for a call that never completed. Check
    ``status`` rather than ``output`` to tell those apart — a task that genuinely
    returned ``None`` is indistinguishable here otherwise."""

    status: str
    """``"completed"``, ``"failed"`` or ``"running"``.

    A bare string on purpose, for now: these three values are the read layer's existing
    per-*call* vocabulary, which is a different and narrower set than
    :class:`~satay.journal.events.RunStatus`, and the codebase carries two more
    un-enumerated status vocabularies besides (attempt-level, and the control plane's
    ``cancelling``/``accepted``). Enumerating one in isolation would imply a
    consistency that does not exist yet."""

    attempts: int
    duration_seconds: float | None
    ordinal: int | None = None
    key: str | None = None
    map_group: str | None = None

    child_run_id: str | None = None
    """For a ``start_child`` call, the child's own run id — pass it back to
    :func:`inspect` to read the child's calls. ``output`` is the child's recorded return
    value, read from the child's journal; when a child *failed*, ``status`` says so and the
    reason is in the child's own inspection, since the parent's journal does not carry it."""

    first_seq: int = 0


@dataclass(frozen=True, slots=True)
class RunInspection:
    """What one run recorded: its identity, its outcome, and its durable calls."""

    run_id: str
    workflow_name: str
    status: RunStatus
    calls: tuple[RecordedCall, ...] = field(default_factory=tuple)
    """Every durable call, in the order it was scheduled.

    Schedule order, not the identity-sorted order :func:`satay.control.views.compare`
    returns: a reader following what the run *did* wants the sequence it happened in."""

    output: Any = None
    """The run's recorded output, or ``None`` if it has not completed."""

    error: Mapping[str, Any] | None = None
    """For a failed run, the recorded ``{"type", "message", "traceback"}``; otherwise
    ``None``. Reported rather than raised — the caller asked what happened, and a read
    that raises the failure it was asked about is harder to use, not safer."""

    code_version: str | None = None
    forked_from: Mapping[str, Any] | None = None
    """For a forked run, its ``{"source_run_id", "fork_point_seq"}`` lineage."""

    def call(self, identity: str) -> RecordedCall | None:
        """The call with this identity, or ``None``. A convenience over ``calls``."""
        for recorded in self.calls:
            if recorded.identity == identity:
                return recorded
        return None


def _recorded_args(recorded: Any) -> tuple[Any, ...]:
    """The recorded positional arguments as a tuple, tolerating a redacted value.

    ``TaskScheduled.input_ref`` holds ``encode(list(args))``, so the normal case is a
    list. The guard is not defensive noise: a caller-supplied redactor whose patterns
    match ``input`` masks the whole slot to a *string*, and ``tuple("***REDACTED***")``
    would silently explode it into one element per character.
    """
    if recorded is None:
        return ()
    if isinstance(recorded, list | tuple):
        return tuple(recorded)
    return (recorded,)


async def inspect(
    run_id: str,
    *,
    store: Store | None = None,
    redactor: Redactor | None = None,
) -> RunInspection:
    """Read back what a run recorded, without forking it and without re-executing it.

    Works on a run in any state — a read is not a fork, so the terminal-only rule of
    ADR-0004 does not apply; an unfinished run simply reports the calls it has recorded
    so far. Raises :class:`LookupError` for an unknown run id, so catching it needs no
    import.

    Redacted by default, with the same default patterns as every HTTP read
    (:class:`~satay.redaction.Redactor`). Pass ``redactor=`` to substitute your own;
    there is deliberately no way to ask for unredacted output, matching the read API,
    where the absence of such a path is the guarantee (N18).
    """
    from satay.api.primitives import _default_store
    from satay.control import views
    from satay.journal.events import RunStatus
    from satay.redaction import Redactor

    resolved = store if store is not None else _default_store()
    view = await views.run_calls(resolved, run_id)
    view = (redactor or Redactor()).redact(view)

    summary = view["summary"]
    calls = tuple(
        RecordedCall(
            identity=call["identity"],
            task_name=call["task_name"],
            args=_recorded_args(call["input"]),
            output=call["output"],
            status=call["status"],
            attempts=call["attempts"],
            duration_seconds=call["duration_seconds"],
            ordinal=call.get("ordinal"),
            key=call.get("key"),
            map_group=call.get("map_group"),
            child_run_id=call.get("child_run_id"),
            first_seq=call.get("first_seq", 0),
        )
        for call in view["calls"]
    )
    return RunInspection(
        run_id=summary["run_id"],
        workflow_name=summary["workflow_name"],
        status=RunStatus(summary["status"]),
        calls=calls,
        output=view["output"],
        error=view["error"],
        code_version=summary.get("code_version"),
        forked_from=summary.get("forked_from"),
    )
