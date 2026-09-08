# ADR-0043 — One durable agent loop as an on-ramp, in `satay.agent`

- **Status:** Accepted
- **Date:** 2026-09-08
- **Deciders:** Jian (leejianrong2@gmail.com)

Implements decision 3 of
[ADR-0041](0041-monetisation-revisited-replay-eval-as-product.md) ("a single durable agent
loop ships as an on-ramp — one blessed pattern, not a framework"), which narrows the
no-agent-abstraction non-goal of [ADR-0025](0025-positioning-agents-first.md) §4. Holds the
line ADR-0025 §4 and [ADR-0032](0032-products-on-top-pipeline-graph-builder.md) draw against
frameworks and graph-shaped core creep.

## Context

The reason-act-observe loop — ask a model, run the tools it requests, feed the results back,
repeat until it answers — is the shape almost every agent consumer builds. ADR-0025 §4 said
teach it in examples and promote it only once it had been hand-rolled enough to know the
shape. ADR-0041 §3 makes that call: ship **one** blessed loop so a builder lands in ten
minutes, and authorise exactly that one — "if it grows a second and third variant, that is a
new decision, not a licence to build a framework."

The hazard is the same one ADR-0032 guards against for the pipeline-graph builder: a loop
that quietly accretes a tool registry, a planner, provider adapters, or retry/repair policy
becomes the LangGraph-scale surface ADR-0025 §4 rejected, and entangles the runtime so it is
no longer releasable without it. So the question is how thin the loop can be while still
being the durable on-ramp.

## Decision

**1. Ship one loop, `satay.agent.agent_loop`, and nothing else agent-shaped.** It takes a
`model` callable, a `tools` mapping, a `prompt`, and a `max_steps` bound; it runs the loop
and returns an `AgentResult`. One shape, one entry point. A second variant is a new ADR.

**2. It is a submodule, not a sixth top-level primitive.** `satay.agent` is imported
explicitly (`from satay.agent import agent_loop`), and is deliberately **not** added to the
five top-level primitives or to `satay.__all__`. ADR-0041 §3 calls it a watched on-ramp, and
ADR-0025 §4's "Satay ships the five durable primitives and nothing agent-shaped" is narrowed,
not erased — keeping the loop one import away from the top level is how the surface still says
which things are the runtime and which is the on-ramp on top of it. (This is the opposite
choice from [ADR-0042](0042-replay-eval-capability.md)'s `replay_eval`, which *is* top-level,
because evaluation is the product centre while the loop is an on-ramp to it.)

**3. Durability is inherited, not built.** The loop imports nothing from `satay` — it is pure
control flow over awaitables. It becomes durable only because the caller makes `model` and
each tool a `@satay.task` and calls the loop inside a `@satay.workflow`: then every model
turn and tool call is a recorded durable call, a crash mid-loop resumes with them replayed as
journal hits, and a builder pays for a step once. This is the strongest possible form of the
"releasable with it deleted" test (ADR-0032): there is no reverse dependency to sever because
there is no dependency at all.

**4. No provider adapter, no dispatch framework, no graph.** The caller maps their provider's
reply into a neutral `AgentStep` (assistant text plus parsed `ToolCall`s) inside their own
model task; Satay never parses a wire format. `tools` is a plain `{name: task}` mapping the
caller owns; the loop dispatches by name and raises `UnknownToolError` on a miss rather than
growing a retry/repair policy to hide it. There is no planner and no second loop.

**5. The loop always terminates.** `max_steps` (default `MAX_STEPS_DEFAULT`) bounds the model
turns, because an unbounded agent that keeps asking for tools would append to its journal
forever — a durability problem, not just a cost one. Reaching it returns
`stop_reason == "max_steps"` rather than raising, so the caller decides what a budget
exhaustion means.

**6. The loop body is deterministic, so replay is exact.** Its branches turn only on recorded
task outputs (`turn.is_final`, `turn.tool_calls`, `call.name`); it touches no clock,
randomness, or I/O. The one caller obligation replay imposes is that the model task be
annotated `-> AgentStep`, so a resumed run rehydrates the turn against its return annotation
(ADR-0005) and the loop can read `turn.tool_calls` after a crash.

## Consequences

- **ADR-0041 §3 is met at its stated maximum and no further.** One loop, submodule-scoped,
  deletable, framework-free. Growing a second shape is a new decision.
- **The on-ramp feeds the product.** An agent run is an ordinary run, so
  `satay.fork`/`replay_eval`/`diff` (ADR-0028/0042/0034) apply to it unchanged — replay an
  agent's last turn under a cheaper model, diff the answer, gate the cost. The example
  demonstrates exactly this, which is the point of putting the loop next to the wedge.
- **It is a watched surface.** Feature requests will pull toward tool schemas, provider
  adapters, planners, and a second loop. The default answer is no; each is a separate ADR
  against the ADR-0025 §4 / ADR-0032 line.
- **The neutral shapes are a small, owned vocabulary.** `AgentStep`, `ToolCall`,
  `ToolInvocation`, `AgentResult` are stdlib frozen dataclasses in the house style; they are
  not a provider abstraction and must not grow provider-specific fields.

## Alternatives considered

- **Keep the loop in examples only** — rejected by ADR-0041 §3: the shape is stable enough and
  hand-rolled often enough that the ten-minute on-ramp is worth one blessed helper. The
  example stays as the teaching artefact and the durability demo.
- **Make it a top-level primitive (`satay.agent_loop`)** — rejected: it would read as a sixth
  durable primitive beside `start`/`sleep`/…, blurring the ADR-0025 §4 line the submodule
  keeps crisp. Discoverability costs one import.
- **Add tool schemas / a registry / provider adapters** — rejected as the LangGraph-scale
  surface ADR-0025 §4 refuses; tools are the caller's own tasks and their shapes the caller's
  own concern.
- **Feed an unknown-tool or tool error back to the model automatically** — rejected as the
  first step toward a retry/repair framework. The loop raises; recovery is the caller's
  workflow to write.
- **Let the loop run unbounded until the model stops** — rejected: a durable loop must
  terminate, and an unbounded one turns a stuck agent into an unbounded journal.

🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_01Dg1niioKdtQhxpnZKvtnvP
