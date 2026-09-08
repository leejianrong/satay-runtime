# ADR-0042 — Replay-based evaluation: a thin composition, not a new mechanism

- **Status:** Accepted
- **Date:** 2026-09-08
- **Deciders:** Jian (leejianrong2@gmail.com)

Implements decision 1 of
[ADR-0041](0041-monetisation-revisited-replay-eval-as-product.md) ("the paid product is
replay-based evaluation"), which promotes evaluation from an
[ADR-0025](0025-positioning-agents-first.md) §4 cookbook example to a first-class
capability. Builds entirely on `fork` ([ADR-0028](0028-fork-from-code-input-override.md)),
`inspect` ([ADR-0033](0033-reading-a-run-without-forking.md)), `diff`
([ADR-0034](0034-structural-diff-of-two-runs.md)) and the usage roll-up
([ADR-0035](0035-usage-rollup-and-a-redaction-word-boundary-fix.md)). Adds no runtime
mechanism.

## Context

ADR-0041 names the differentiated asset — fork-from-a-real-prefix + deterministic replay +
call-by-call diff — and says evaluation is that asset pointed at a question: *does this new
prompt, model, or code version change the answer, and what does it cost?* Every piece
needed to answer that question already shipped and is public. What did not exist was the
composition that ties them into one call, and a CI-gating verdict over the result.

The hazard is scope. ADR-0025 §4 and
[ADR-0032](0032-products-on-top-pipeline-graph-builder.md) both warn against graph-shaped
core creep and agent frameworks, and ADR-0032 fixes the test: the runtime must stay
releasable with the product-on-top deleted. An eval capability that grew its own event
type, its own store column, or a registry of "eval runs" would fail that test. So the
question this ADR settles is not *whether* to add evaluation but *how thin* it is allowed
to be.

## Decision

**1. Evaluation is a composition module, `satay.eval`, in the core.** It imports only the
public API (`fork`, `inspect`, `diff`) and one core helper (`satay.valuediff.diff_values`,
already shared with the HTTP compare view). Nothing in the core imports it back, so
deleting the file leaves the runtime unchanged and releasable — the ADR-0032 test, applied
to eval. It is core, not `satay[studio]`, because ADR-0041 §4 forbids the paywall on any
core capability; the paid product is *hosting* this loop for a team, never the loop itself.

**2. Two entry points, for the two CI shapes.** `replay_eval(baseline_run_id, …)` is the
active one: fork the baseline at a chosen point, optionally under a changed
`workflow_input`, drive the fork to terminal, and return an `EvalReport`.
`compare_runs(baseline_run_id, candidate_run_id, …)` is the read-only half: build the same
report from two runs that already exist, forking and writing nothing. The CLI gate and a
team whose candidate was produced another way use the second.

**3. There is no new "model" or "version" axis.** A changed prompt travels through
`workflow_input=`; a changed model or code version travels through `fork`'s existing
re-execution — import the new code before calling, and the calls after the fork point run
under it. Adding a dedicated axis would duplicate what fork already does and invite the
node-metadata creep ADR-0032 rejects.

**4. `EvalReport` reuses the existing value types.** The workflow-output delta is a
`ValueDiff` (ADR-0034), the call-by-call alignment is a `RunDiff`, and the cost delta is a
per-key map over the `RunInspection.usage` roll-up (ADR-0035). The only new dataclasses are
`EvalReport` itself and `GateResult`. No new event, no new column, no new identity scheme.

**5. `gate` judges two objective axes only: output regression and cost regression.**
`expect_output` ∈ `unchanged`/`changed`/`any` — `unchanged` (the default) is the
regression-test lens where a changed *or unconfirmable* output fails; `changed` is the
intended-eval lens where an unchanged output fails; `any` gates cost alone.
`max_usage_increase` caps the per-key cost delta, as a scalar over every key or a mapping
of per-key caps. Satay judges identity and cost; it never judges *quality*, which is
domain-specific and stays the caller's to assert. This is the honest, demoable claim
ADR-0041 rests on.

**6. The CI-gating CLI entry point is read-only.** `satay eval BASELINE CANDIDATE` compares
two runs that already exist and exits non-zero on a regression. It reads only, so the core
`argparse` CLI needs no workflow import (ADR-0016) — producing the candidate, which *does*
need the code under test in scope, is `replay_eval`'s job from the caller's own harness.

**7. Redaction is inherited, not re-decided.** Both entry points read through `inspect` and
`diff`, so they are redacted by default with the same override, and a value masked in the
journal itself surfaces as `ValueDiff.redacted` rather than being counted equal — exactly
the ADR-0033/0034 contract. The workflow-output delta is computed over the read-redacted
outputs; with the default patterns an answer that is itself the value compares intact, and
a secret-named leaf reports `redacted`. Moving that one comparison pre-redaction (as the
call diff already is, inside `views.compare`) is a possible later refinement, not a
blocker.

## Consequences

- **ADR-0041 §1 is met with no new core mechanism.** Evaluation is now first-class in the
  public surface (`replay_eval`, `compare_runs`, `gate`, `EvalReport`, `GateResult`) while
  the runtime stays releasable with `satay/eval.py` and the `satay eval` verb deleted.
- **The hosted eval plane inherits this shape.** The tier-1/tier-2 plane (ADR-0041 §2)
  hosts these reads and drives; the report and gate are the contract it exposes, so the
  hosted surface is a transport over an already-tested local capability, not a reimplementation.
- **A parked fork yields a partial report.** `replay_eval` drives the fork; if it parks
  with nothing to wake it, `candidate_status` is non-terminal and the deltas are partial.
  Reported honestly via the status field rather than hidden — the same PARKED reality
  `fork` already has (ADR-0030).
- **Cost gating depends on self-reported usage.** A run that never called
  `ctx.record_model_usage` has an empty roll-up and an empty cost delta, so the cost gate is
  vacuously satisfied. That is correct: Satay gates what was recorded, and records only what
  a task self-reports (ADR-0008).

## Alternatives considered

- **Keep evaluation as a cookbook example only** — rejected by ADR-0041 §1: the product
  centre cannot be a file a user copies and maintains. The example stays (it is the demo),
  but the loop it teaches is now a supported call.
- **Add an `EvalRun` event type / an eval-run registry** — rejected: it is the graph-shaped
  core creep ADR-0032 exists to stop, and it would break the "releasable with the product
  deleted" test. An eval is two ordinary runs and a read; nothing durable needs to know it
  happened.
- **Put `satay eval` behind `satay[studio]`** — rejected: ADR-0041 §4 keeps the core loop
  free, and a read-only compare over an existing journal has no studio dependency to justify
  the move.
- **Have `gate` score answer quality** — rejected: quality is domain-specific and not
  mechanically defensible. Gating identity and cost is the claim Satay can make honestly and
  demo in one command.

🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_01Dg1niioKdtQhxpnZKvtnvP
