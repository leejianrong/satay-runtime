"""Integration: replay-based evaluation over the public surface (ADR-0041).

Drives real workflows against a temp SQLite store through the public API and asserts only
observable outcomes — the :class:`~satay.EvalReport` fields, the :class:`~satay.GateResult`
verdict, and run status — never replay internals (ADR-0011). ``replay_eval`` is composition
over ``fork`` + ``inspect`` + ``diff``; these tests pin the behaviour a caller and the CI
gate depend on: which calls re-ran, how output and cost moved, and how the gate reads that.

The workflow is deliberately arithmetic, not a fake LLM: the deltas below are exact, so a
regression in the roll-up or the diff shows up as a wrong number rather than a soft one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

import satay
from satay.journal.store import SQLiteStore


@dataclass(frozen=True)
class Job:
    """Plain, forkable workflow input. ``factor`` is the knob an eval turns."""

    seed: int
    factor: int


@satay.task()
async def load(seed: int) -> int:
    """The prefix call: fixed cost, reused byte-identical by a fork placed after it."""
    satay.task_context().record_model_usage(input_tokens=100, output_tokens=10, usd=0.01)
    return seed


@satay.task()
async def score(base: int, factor: int) -> int:
    """The call the evals re-run. Its cost scales with ``factor`` — the whole measurement."""
    satay.task_context().record_model_usage(
        input_tokens=factor, output_tokens=1, usd=round(factor * 0.001, 6)
    )
    return base * factor


@satay.workflow
async def run_job(job: Job) -> dict[str, Any]:
    """load(seed) → score(base, factor). ``factor`` is passed straight from the input, so a
    fork before ``score`` under a new input reaches it (ADR-0028)."""
    base = await load(job.seed)
    total = await score(base, job.factor)
    return {"seed": job.seed, "factor": job.factor, "total": total}


@pytest.fixture
def store() -> Any:
    """A fresh in-memory journal per test."""
    opened = SQLiteStore.open(":memory:")
    try:
        yield opened
    finally:
        opened.close()


async def _baseline(store: SQLiteStore) -> str:
    """Record the baseline run: Job(seed=5, factor=2)."""
    handle = satay.start(run_job, Job(seed=5, factor=2), store=store)
    result = await handle.result()
    assert result == {"seed": 5, "factor": 2, "total": 10}
    return handle.run_id


async def test_replay_eval_reports_output_and_cost_deltas(store: SQLiteStore) -> None:
    """A sharper input re-runs only ``score``; output and cost move by exact amounts."""
    baseline_id = await _baseline(store)

    report = await satay.replay_eval(
        baseline_id, before_task="score", workflow_input=Job(seed=5, factor=4), store=store
    )

    assert report.baseline_run_id == baseline_id
    assert report.candidate_run_id != baseline_id
    assert report.candidate_status == "completed"

    # Output: factor and the total it drives both change; seed does not.
    assert report.output.changed is True
    assert set(report.output.paths) == {".factor", ".total"}
    assert report.candidate_output == {"seed": 5, "factor": 4, "total": 20}

    # Only the call after the fork point re-ran; load was reused.
    assert [call.identity for call in report.calls.changed] == ["score:0"]

    # Cost delta isolates the re-run call: load's usage is identical on both sides.
    assert report.usage_delta["usd"] == pytest.approx(0.002)  # 0.004 - 0.002
    assert report.usage_delta["input_tokens"] == 2  # 4 - 2
    assert report.usage_delta["output_tokens"] == 0


async def test_gate_reads_one_report_two_ways(store: SQLiteStore) -> None:
    """The same report is a pass for an intended eval and a fail for a regression test."""
    baseline_id = await _baseline(store)
    report = await satay.replay_eval(
        baseline_id, before_task="score", workflow_input=Job(seed=5, factor=4), store=store
    )

    # Intended change: a changed output is the pass.
    changed = satay.gate(report, expect_output="changed")
    assert changed.passed is True
    assert changed.reasons == ()

    # Regression test: the same changed output is the failure, with a reason to print.
    unchanged = satay.gate(report, expect_output="unchanged")
    assert unchanged.passed is False
    assert any("output changed" in reason for reason in unchanged.reasons)


async def test_gate_cost_cap(store: SQLiteStore) -> None:
    """The cost gate fails a rise above the cap and passes a rise within it."""
    baseline_id = await _baseline(store)
    report = await satay.replay_eval(
        baseline_id, before_task="score", workflow_input=Job(seed=5, factor=4), store=store
    )

    over = satay.gate(report, expect_output="any", max_usage_increase={"usd": 0.0})
    assert over.passed is False
    assert any("usd" in reason for reason in over.reasons)

    within = satay.gate(report, expect_output="any", max_usage_increase={"usd": 1.0})
    assert within.passed is True

    # A scalar cap applies to every key; here input_tokens rose by 2, so cap 1 fails.
    scalar = satay.gate(report, expect_output="any", max_usage_increase=1)
    assert scalar.passed is False


async def test_replay_eval_cheaper_input_passes_cost_gate(store: SQLiteStore) -> None:
    """A change that lowers cost passes a 'must not increase' gate (a decrease never fails)."""
    baseline_id = await _baseline(store)
    report = await satay.replay_eval(
        baseline_id, before_task="score", workflow_input=Job(seed=5, factor=1), store=store
    )

    assert report.usage_delta["usd"] < 0
    verdict = satay.gate(report, expect_output="any", max_usage_increase={"usd": 0.0})
    assert verdict.passed is True


async def test_compare_runs_reads_two_existing_runs_without_forking(store: SQLiteStore) -> None:
    """The read-only half: two independently started runs compare with no fork or write."""
    a = satay.start(run_job, Job(seed=5, factor=2), store=store)
    b = satay.start(run_job, Job(seed=5, factor=4), store=store)
    assert await a.result() == {"seed": 5, "factor": 2, "total": 10}
    assert await b.result() == {"seed": 5, "factor": 4, "total": 20}

    report = await satay.compare_runs(a.run_id, b.run_id, store=store)

    assert report.baseline_run_id == a.run_id
    assert report.candidate_run_id == b.run_id
    assert report.output.changed is True
    # load(5) is identical in both; only score differs.
    assert [call.identity for call in report.calls.changed] == ["score:0"]
    assert report.usage_delta["usd"] == pytest.approx(0.002)


async def test_compare_runs_unknown_run_raises_lookup_error(store: SQLiteStore) -> None:
    """An unknown run id is a plain :class:`LookupError`, catchable without an import."""
    baseline_id = await _baseline(store)
    with pytest.raises(LookupError):
        await satay.compare_runs("does-not-exist", baseline_id, store=store)


async def test_gate_rejects_unknown_expect_output(store: SQLiteStore) -> None:
    """A typo'd ``expect_output`` fails loudly rather than silently gating nothing."""
    baseline_id = await _baseline(store)
    report = await satay.compare_runs(baseline_id, baseline_id, store=store)
    with pytest.raises(ValueError):
        satay.gate(report, expect_output="bogus")


async def test_compare_run_against_itself_is_unchanged(store: SQLiteStore) -> None:
    """A run compared to itself is the clean baseline: nothing changed, gate passes."""
    baseline_id = await _baseline(store)
    report = await satay.compare_runs(baseline_id, baseline_id, store=store)

    assert report.output.changed is False
    assert report.calls.changed == ()
    assert all(delta == 0 for delta in report.usage_delta.values())
    assert satay.gate(report, expect_output="unchanged", max_usage_increase=0).passed is True
