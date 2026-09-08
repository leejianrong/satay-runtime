# ADR-0040 — Monetisation revisited: replay-based evaluation is the product, and the hosting ceiling rises

- **Status:** Proposed
- **Date:** 2026-09-08
- **Deciders:** Jian (leejianrong2@gmail.com)

Revises the monetisation and scope posture set in
[ADR-0025](0025-positioning-agents-first.md) (agents first, debugger as wedge) and
[ADR-0026](0026-license-and-hosted-journal-plane.md) (Apache-2.0 core, hosted plane
capped at tier 1). It keeps both ADRs' foundations — the debugger wedge, the
permissive licence, write-time redaction as the gating prerequisite — and changes two
things: **what the paid product actually is**, and **how high the hosting ceiling is
allowed to go**. It does not touch the core-dependency boundary (ADR-0013/0016) or the
single-writer model; those are re-timed nowhere and hold as written.

## Context

ADR-0025 correctly diagnosed that durability is a commodity in 2026 and pivoted the
*pitch* to the debugger. ADR-0026 then designed a *hosting* business around that wedge
and, out of well-founded caution about credentials and compliance, capped it at tier 1
(journal ingest, retention, hosted Studio, sharing, cost reporting) and explicitly
ruled hosted execution first-in.

Both decisions were sound as risk management. Read as a plan to make money, they leave
two gaps:

**The pivot stopped at marketing.** ADR-0025 changed the sentence on the box from
"durable" to "debugger" but left the roadmap, the non-goals, and the eventual paid
surface unchanged. The genuinely rare, hard-to-copy asset in this codebase is not the
journal and not durability — it is **fork-from-a-real-prefix + deterministic replay +
call-by-call diff** (ADR-0001, ADR-0028, ADR-0034). Every incumbent in AI
observability (LangSmith, Langfuse, Arize, Braintrust) reconstructs runs from *logged
traces*. Satay can *re-execute* a recorded run against changed code, prompts, models,
or tools and show exactly what diverged. That is counterfactual replay, and it is the
thing worth building a company on. It is currently framed as a debugger feature and a
cookbook example rather than as the product.

**The hosting ceiling was set by liability, not by revenue.** ARR in this category
scales when the vendor becomes infrastructure the customer cannot rip out, which is
hosted control and, eventually, hosted execution — precisely the tiers ADR-0026
deferred. Tier 1 alone is a churnable convenience layer over a free tool. That is a
fine first step and a poor destination.

The stated goal for this direction is a **profitable SaaS**, not merely a sustainable
open-source project. That goal justifies revisiting both gaps now, while there is no
hosting code to unwind and the exit remains cheap.

## Decision

**1. The paid product is replay-based evaluation and debugging, not hosting-as-such.**
The monetised surface is: record real runs, fork any run, replay it against a changed
prompt / model / tool / code version, diff outputs call-by-call, and gate CI on
output *and* cost regressions — with shared retained history and alerting around it.
Hosting is the delivery mechanism for a team; the *value* being sold is
counterfactual replay applied to evaluation. This promotes `diff` (ADR-0034) and
usage roll-up (ADR-0035) from features to the centre of the product, and promotes
evaluation from an ADR-0025 §4 "cookbook example" to a first-class paid capability.

**2. The hosting ceiling rises from tier 1 to tier 2, and tier 3 is reopened as a
sequenced goal rather than a rejected one.** ADR-0026's tiers still describe the
credential surface accurately; this ADR re-times them:

- **Tier 1** (ingest, retention, hosted Studio, sharing, cost) — ships first, as
  ADR-0026 already planned. No change.
- **Tier 2** (hosted control plane: `start` / `cancel` / `send_event` / `fork` as a
  service) — pulled forward from "later decision" to **the intended second step**. It
  holds no model keys or database credentials (workflows still run on customer infra),
  so it buys stickiness at a fraction of tier 3's compliance cost.
- **Tier 3** (hosted execution) — no longer rejected, but **explicitly gated** on a
  compliance surface Satay does not have today (data residency, key custody, a
  security posture worth auditing). It is the endgame, sequenced after tier 2 proves
  demand, and it will need its own ADR when its prerequisites are actually funded.

**3. A single durable agent loop ships as an on-ramp — one blessed pattern, not a
framework.** ADR-0025 §4's ban on agent abstractions is narrowed, not lifted. The
non-goals it protects (no provider adapters, no graph DSL, no LangChain-scale
integration breadth, no loop *framework*) all hold. What changes: the durable
agent loop, which every consumer currently hand-rolls, is promoted from example to a
thin, opinionated primitive so that a builder lands in ten minutes rather than an
afternoon. One loop, one shape, deletable without touching the runtime. If it grows a
second and third variant, that is a new decision, not a licence to build a framework.

**4. The open-source core stays Apache-2.0 and stays the funnel.** ADR-0026 §1 is
reaffirmed without qualification. No capability — multi-worker, Postgres, blob GC,
the agent loop, replay, diff — is withheld from a self-hosted user. Free distribution
of the runtime and local Studio is the growth engine; the paid product is the hosted,
team-scale, evaluation-and-history plane on top. The paywall is never on the core.

**5. Write-time redaction (ADR-0029) remains the hard prerequisite for every paid
tier.** Reaffirmed and, if anything, made more load-bearing: tiers 1–3 all move
journals to a store this project operates, and read-time redaction protects the API
response, not the store. No journal leaves a customer process for a Satay-operated
store before write-time redaction lands. This is the first thing built, ahead of any
tier.

## Consequences

- **The competitive frame changes from "durable execution" to "evaluation and
  explainability for non-deterministic AI systems."** The comparison set becomes
  Braintrust / LangSmith / Langfuse, not Temporal / DBOS / Restate. The defensible
  claim is mechanical and demoable: *we re-run your actual recorded run against the
  change; we do not approximate it from logs.* This supersedes ADR-0025's
  consolidation claim ("one model covers both") as the primary message, though that
  claim remains true and usable as a secondary.
- **Tier 2 becomes a design task, not just a later decision.** The control-plane
  contract crossing a network boundary inherits ADR-0026 §6's versioning requirement,
  and it must stay as free of Satay-specific tenancy assumptions as the ingest
  contract, so the schema-agnostic plane (ADR-0026 §5, sibei-flow as second tenant)
  is preserved.
- **Evaluation moves onto the critical path**, joining collect-mode fan-out
  (ADR-0027), which ADR-0025 already put there for the same "draft N, keep the best"
  reason. Regression-gating in CI depends on replay against a new code version, which
  depends on the code-version stamp (ADR-0023) and structural diff (ADR-0034) already
  in the core; the new work is the hosted eval-run and gating surface, not new core
  mechanism.
- **Tier 3 carries an explicit compliance debt.** Reopening it is a statement of
  intent, not a commitment to build soon. Anyone picking it up inherits key custody,
  data residency, and an audit posture as prerequisites, and must write the ADR that
  scopes them before code.
- **The agent-loop on-ramp is a watched surface.** ADR-0025 §4's pull toward a
  framework is real; promoting one loop is the maximum this ADR authorises. The
  runtime must remain releasable with the loop deleted, exactly as ADR-0032 requires
  of the pipeline-graph builder.
- **New target groups become addressable**, in rough order of nearness: AI platform
  and eng teams already paying for eval/observability (the direct swap-in for the
  replay-eval product); AI-native startups wanting durable execution with no ops (the
  tier-2, later tier-3 buyer); regulated and audit-heavy AI teams, for whom "we hold a
  redacted, replayable journal and never your keys" is a moat rather than a
  convenience (write-time redaction is the feature sold to them); other product
  companies renting the schema-agnostic plane as embedded run-history for their own
  users (B2B2C); and governance buyers purchasing prompt/model change management as
  risk control rather than as a developer tool.
- **The cheap-exit property from ADR-0026 §5 is preserved.** If Satay's own launch
  does not land, decisions 1–2 still transfer to the sibei-flow plane, and nothing
  built under write-time redaction or the versioned contracts is wasted.

## Alternatives considered

- **Keep ADR-0026's tier-1-only ceiling** — rejected for the profit goal: tier 1 is a
  churnable convenience over a free tool, not sticky infrastructure. It remains the
  correct *first* step, which is why decision 2 keeps it and adds above it rather than
  replacing it.
- **Go straight to hosted execution (tier 3) to maximise ARR** — rejected as ADR-0026
  rejected it: it inherits model keys, database credentials, and a compliance surface
  the project has none of today. Decision 2 sequences it last behind tier 2 instead of
  ruling it out.
- **Ship a full agent framework to compete with LangGraph** — rejected as ADR-0025 §4
  rejected it: it reverses the integration-breadth non-goal and trades the one
  differentiated asset (real journal replay) for an ecosystem race. Decision 3 takes
  only the single on-ramp loop, not the framework.
- **Paywall the platform capabilities (open-core over multi-worker / Postgres)** —
  rejected as ADR-0026 §1 rejected it: gating exactly the capabilities the platform
  phase needs to be freely adoptable would stall that phase. The funnel depends on the
  core staying free.
- **Sell durability and observability as-is, competing on price** — rejected: it is
  the commodity lane ADR-0025 already identified as crowded, and it abandons the only
  mechanism (counterfactual replay) that a well-funded incumbent cannot cheaply copy.

🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_01CVZyDBpxY6pwfkn3K3bDG4
