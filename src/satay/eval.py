"""``satay.eval`` — replay-based evaluation: fork a real run, replay it against a change,
and gate CI on output *and* cost regressions (ADR-0041).

This module is the wedge ADR-0041 §1 promotes from an ADR-0025 §4 "cookbook example" to a
first-class capability: the rare, hard-to-copy thing in this codebase is
**fork-from-a-real-prefix + deterministic replay + call-by-call diff**, and *evaluation*
is that mechanism pointed at a question — "does this new prompt / model / code version
change the answer, and what does it cost?".

**It is composition, not mechanism.** Everything here is a thin orchestration over the
existing public surface: :func:`satay.fork` re-cuts and re-drives the run,
:func:`satay.inspect` reads back what each side recorded (including
:attr:`RunInspection.usage`, ADR-0035), :func:`satay.diff` aligns the two call-by-call,
and :func:`satay.valuediff.diff_values` locates where the workflow *outputs* differ. No
new runtime mechanism, no new event, no graph shape. Delete this file and the runtime is
unchanged and still releasable (the ADR-0032 test, applied to eval): it imports only the
public API and one core helper, and nothing in the core imports it back.

**Two entry points, for the two CI shapes.**

- :func:`replay_eval` is the *active* one: fork a recorded baseline at a chosen point,
  optionally under a changed input, drive the fork to terminal, and return an
  :class:`EvalReport`. Change a prompt/model/code *version* the way :func:`satay.fork`
  already allows — by importing the new code before you call, so the re-executed calls
  after the fork point run under it. There is deliberately no new "model" or "version"
  axis here; fork's re-execution already carries it.
- :func:`compare_runs` is the *read-only* half: given two runs that already exist, build
  the same :class:`EvalReport` without forking or writing anything. This is what the
  ``satay eval`` CLI gate uses, and what a team whose candidate run was produced some
  other way (its own fork, a fresh :func:`satay.start`) calls.

:func:`gate` turns a report into a pass/fail verdict on two *objective* axes — output
regression and cost regression. Satay judges identity and cost; it never judges "better".
Quality is domain-specific and stays the caller's to assert.

Redacted by default on the same terms as :func:`satay.inspect` / :func:`satay.diff`
(ADR-0033/0034): reads apply the default :class:`~satay.redaction.Redactor`, values come
back decoded but untyped, and a leaf masked in the journal itself (write-time redaction,
ADR-0029) is reported as ``redacted`` rather than silently counted equal.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from satay.api.diffing import RunDiff, ValueDiff, diff
from satay.api.fork import fork
from satay.api.inspection import RunInspection, inspect
from satay.valuediff import diff_values

if TYPE_CHECKING:
    from satay.config import EffectSafety, NondeterminismPolicy, VersionMismatchPolicy
    from satay.journal import Store
    from satay.journal.events import RunStatus
    from satay.redaction import Redactor
    from satay.testing.clock import Clock
    from satay.testing.faults import FaultInjector
    from satay.testing.rng import Rng


class _Unset:
    """The type of :data:`_UNSET`."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<unset>"


#: Default for ``workflow_input=`` on :func:`replay_eval`: leave it to :func:`satay.fork`
#: to inherit the source run's recorded input. A sentinel rather than ``None`` because
#: ``None`` is a valid workflow input, and because reaching for ``fork``'s own private
#: inherit-sentinel would couple this module to fork internals for no gain — when the
#: caller supplies nothing we simply do not pass the argument on.
_UNSET: Any = _Unset()

#: The valid ``expect_output`` verdicts for :func:`gate`.
_EXPECT_OUTPUT = ("unchanged", "changed", "any")


@dataclass(frozen=True, slots=True)
class EvalReport:
    """One replay-evaluation: a baseline run against a candidate, output and cost.

    Built by :func:`replay_eval` (which forks and drives the candidate) or by
    :func:`compare_runs` (which only reads two runs that already exist). It is data only —
    :func:`gate` turns it into a verdict.
    """

    baseline_run_id: str
    candidate_run_id: str

    baseline_status: RunStatus
    candidate_status: RunStatus

    output: ValueDiff
    """Where the two runs' **workflow outputs** differ, jq-style paths, reusing the same
    :class:`~satay.api.diffing.ValueDiff` shape as a call's output diff. ``changed`` is the
    headline an eval turns on; ``redacted`` says a compared leaf was masked in the journal
    itself, so equality is unknown rather than established.

    Computed over decoded, read-redacted values. With the default redactor a workflow
    output that is the answer itself (a string, or a dict of non-secret fields) compares
    intact; only a leaf whose *field name* matches a secret pattern is masked, and that
    surfaces as ``redacted``. A run recorded under write-time redaction (ADR-0029) has the
    cleartext gone at every layer, which is the honest ``redacted`` case."""

    baseline_output: Any
    candidate_output: Any

    calls: RunDiff
    """The full call-by-call alignment from :func:`satay.diff`. ``calls.changed`` is the
    usual "which calls diverged" view; the paths inside each call's ``output`` diff are
    computed *before* redaction and stay correct even when the values are masked."""

    baseline_usage: Mapping[str, int | float]
    candidate_usage: Mapping[str, int | float]
    usage_delta: Mapping[str, int | float] = field(default_factory=dict)
    """``candidate - baseline`` per usage key (``input_tokens``, ``output_tokens``, a
    self-reported ``usd`` cost, anything else recorded via ``ctx.record_model_usage``). A
    key present on only one side counts the other as ``0``, which is the honest reading of
    "the change added (or removed) a metric". A key the redactor masked is absent from both
    totals and so from the delta: a number that cannot be summed is unknown, not zero
    (matching :attr:`RunInspection.usage`)."""


@dataclass(frozen=True, slots=True)
class GateResult:
    """The verdict :func:`gate` returns: did the candidate regress against the baseline."""

    passed: bool
    reasons: tuple[str, ...] = ()
    """Human-readable lines, one per failed check, empty when :attr:`passed`. Written to be
    printed straight into a CI log — each names the axis and the numbers behind the
    verdict."""


async def compare_runs(
    baseline_run_id: str,
    candidate_run_id: str,
    *,
    store: Store | None = None,
    redactor: Redactor | None = None,
) -> EvalReport:
    """Build an :class:`EvalReport` from two runs that already exist — read only.

    Nothing is forked, driven, or written: this reads both runs back with
    :func:`satay.inspect`, aligns them with :func:`satay.diff`, and diffs their workflow
    outputs. Use it when the candidate run was produced some other way, or from the
    ``satay eval`` CLI. Raises :class:`LookupError` if either run id is unknown.

    Redacted by default; pass ``redactor=`` to substitute your own, exactly as
    :func:`satay.inspect` and :func:`satay.diff` take it.
    """
    baseline = await inspect(baseline_run_id, store=store, redactor=redactor)
    candidate = await inspect(candidate_run_id, store=store, redactor=redactor)
    run_diff = await diff(baseline_run_id, candidate_run_id, store=store, redactor=redactor)
    return _report(baseline, candidate, run_diff)


async def replay_eval(
    baseline_run_id: str,
    *,
    before_task: str | None = None,
    before_ordinal: int | None = None,
    fork_point_seq: int | None = None,
    workflow_input: Any = _UNSET,
    run_id: str | None = None,
    store: Store | None = None,
    redactor: Redactor | None = None,
    injector: FaultInjector | None = None,
    clock: Clock | None = None,
    rng: Rng | None = None,
    effect_safety: str | EffectSafety | None = None,
    nondeterminism: str | NondeterminismPolicy | None = None,
    version_mismatch: str | VersionMismatchPolicy | None = None,
) -> EvalReport:
    """Fork a recorded run, replay it against a change, and report output + cost deltas.

    The active counterpart to :func:`compare_runs`: it forks ``baseline_run_id`` at the
    chosen point (:func:`satay.fork`'s ``before_task`` / ``before_ordinal`` /
    ``fork_point_seq``, same rules), drives the fork to a terminal state, then compares the
    two. The source run is never modified (ADR-0004).

    **Changing the input** is ``workflow_input=`` — the "same run, sharper prompt" story,
    passed straight through to :func:`satay.fork`, so it reaches only the calls *after* the
    fork point and is redacted on the way in under write-time redaction. Omit it to inherit
    the baseline's recorded input (the "same input, new code/model" story).

    **Changing a prompt, model, or code version** needs no argument here: import the new
    code before you call, and the calls re-executed after the fork point run under it,
    exactly as :func:`satay.fork` already re-executes. The
    ``examples/replay_eval_demo.py`` cookbook shows both axes end to end.

    If the fork parks (a timer or event with nothing in this process to wake it), its
    ``result()`` returns :data:`satay.PARKED` and the report reflects the partial
    candidate — :attr:`EvalReport.candidate_status` will not be terminal; check it before
    trusting the deltas. ``store`` / ``redactor`` / ``injector`` / ``clock`` / ``rng`` and
    the three policy settings behave as they do on :func:`satay.fork` and
    :func:`satay.inspect`.
    """
    fork_kwargs: dict[str, Any] = {
        "before_task": before_task,
        "before_ordinal": before_ordinal,
        "fork_point_seq": fork_point_seq,
        "run_id": run_id,
        "store": store,
        "injector": injector,
        "clock": clock,
        "rng": rng,
        "effect_safety": effect_safety,
        "nondeterminism": nondeterminism,
        "version_mismatch": version_mismatch,
    }
    if not isinstance(workflow_input, _Unset):
        fork_kwargs["workflow_input"] = workflow_input

    handle = await fork(baseline_run_id, **fork_kwargs)
    await handle.result()  # drive to terminal (or park); the outcome is read back below
    return await compare_runs(
        baseline_run_id,
        handle.run_id,
        store=store,
        redactor=redactor,
    )


def _report(
    baseline: RunInspection,
    candidate: RunInspection,
    run_diff: RunDiff,
) -> EvalReport:
    """Assemble an :class:`EvalReport` from two inspections and their call diff."""
    raw = diff_values(baseline.output, candidate.output)
    output = ValueDiff(
        changed=bool(raw["changed"]),
        paths=tuple(raw["paths"]),
        redacted=bool(raw["redacted"]),
        truncated=bool(raw["truncated"]),
    )
    return EvalReport(
        baseline_run_id=baseline.run_id,
        candidate_run_id=candidate.run_id,
        baseline_status=baseline.status,
        candidate_status=candidate.status,
        output=output,
        baseline_output=baseline.output,
        candidate_output=candidate.output,
        calls=run_diff,
        baseline_usage=dict(baseline.usage),
        candidate_usage=dict(candidate.usage),
        usage_delta=_usage_delta(baseline.usage, candidate.usage),
    )


def _usage_delta(
    baseline: Mapping[str, int | float],
    candidate: Mapping[str, int | float],
) -> dict[str, int | float]:
    """``candidate - baseline`` for every key present on either side (missing counts 0)."""
    keys = baseline.keys() | candidate.keys()
    return {key: candidate.get(key, 0) - baseline.get(key, 0) for key in keys}


def gate(
    report: EvalReport,
    *,
    expect_output: str = "unchanged",
    max_usage_increase: float | Mapping[str, float] | None = None,
) -> GateResult:
    """Turn an :class:`EvalReport` into a pass/fail verdict on output and cost.

    Two objective checks; both default to permissive so a caller opts into each:

    - **Output.** ``expect_output`` is one of:

      - ``"unchanged"`` (the default, the *regression-test* shape): a changed output is a
        regression. Because equality must be *established*, a masked output
        (:attr:`EvalReport.output` ``.redacted``) also fails — "cannot confirm unchanged"
        is not "unchanged".
      - ``"changed"`` (the *intended-eval* shape): an output that did **not** change is the
        failure — the change under test did nothing.
      - ``"any"``: output is not gated, only reported.

    - **Cost.** ``max_usage_increase`` caps the per-key :attr:`EvalReport.usage_delta`:

      - ``None`` — cost is not gated.
      - a number — the cap applies to **every** usage key; any key rising above it fails.
      - a mapping — a per-key cap; only the named keys are gated (use it to gate ``usd`` or
        ``output_tokens`` alone and ignore the rest).

    A cap of ``0`` means "must not increase at all". A *decrease* never fails. Returns a
    :class:`GateResult`; raises :class:`ValueError` for an unknown ``expect_output``.
    """
    if expect_output not in _EXPECT_OUTPUT:
        raise ValueError(f"expect_output must be one of {_EXPECT_OUTPUT}, not {expect_output!r}")

    reasons: list[str] = []

    if expect_output == "unchanged":
        if report.output.changed:
            where = ", ".join(report.output.paths) or "."
            reasons.append(f"output changed (at {where}); expected it to be unchanged")
        elif report.output.redacted:
            reasons.append(
                "output equality is unknown (a compared value is redacted in the journal); "
                "cannot confirm it is unchanged"
            )
    elif expect_output == "changed" and not report.output.changed:
        reasons.append("output did not change; expected the change under test to alter it")

    reasons.extend(_cost_reasons(report.usage_delta, max_usage_increase))

    return GateResult(passed=not reasons, reasons=tuple(reasons))


def _cost_reasons(
    usage_delta: Mapping[str, int | float],
    max_usage_increase: float | Mapping[str, float] | None,
) -> list[str]:
    """The cost-gate failure lines for one delta against one cap spec (empty if within)."""
    if max_usage_increase is None:
        return []

    if isinstance(max_usage_increase, Mapping):
        caps = {
            key: (key in max_usage_increase, max_usage_increase.get(key, 0.0))
            for key in usage_delta
        }
    else:
        caps = {key: (True, float(max_usage_increase)) for key in usage_delta}

    reasons: list[str] = []
    for key, delta in usage_delta.items():
        gated, cap = caps.get(key, (False, 0.0))
        if gated and delta > cap:
            reasons.append(f"usage {key!r} increased by {delta} (cap {cap})")
    return reasons


__all__ = [
    "EvalReport",
    "GateResult",
    "compare_runs",
    "gate",
    "replay_eval",
]
