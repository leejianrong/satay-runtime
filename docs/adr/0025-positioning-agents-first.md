# ADR-0025 — Positioning and roadmap order: the debugger is the wedge, agents first, platform second

- **Status:** Accepted
- **Date:** 2026-08-05
- **Deciders:** Jian (leejianrong2@gmail.com)

Amends the `D-scope` entry in [CONTEXT.md](../CONTEXT.md)'s decision register (the
vendor-dossier app is no longer the next milestone) and re-times, without changing,
the `SQLite → PostgreSQL → multi-worker` ordering in ARCHITECTURE §9. Reopens
[ADR-0020](0020-composite-failure-semantics.md) as a consequence rather than
superseding it here.

## Context

V1–V8 are merged, the suite is green, and `0.1.0a3` is on PyPI with no launch. What
the docs do not say is **who the first user is**, and several decisions were made as
if that question had an obvious answer.

Two facts make it urgent:

**Durability is a commodity claim in 2026.** Temporal, Restate, DBOS, Inngest and
Hatchet all sell it, and "survives crashes" now reads as table stakes rather than as
a reason to switch. Meanwhile DBOS and Restate occupy the specific lane Satay might
have assumed was empty: durable execution, Postgres, lightweight, local-first, with
a hosted plane on top. The lighter-than-Temporal position is already taken.

**The board says what the real blockers are.** Of 36 open cards, roughly ten are
API-shape and usability gaps, and every one of them was found by somebody trying to
*build* something with Satay rather than by reading it: KAN-491 (the parked-run
worker pattern is not teachable), KAN-477 (no way to read a terminal run's recorded
results without forking), KAN-481 (fork is unusable from code), KAN-476 (two
`ctx.idempotency_key` traps), KAN-520 (`decode()` discards the discriminator, so
union rehydration guesses), KAN-579 (a zero-parameter `@satay.workflow` fails at
drive time rather than at decoration), KAN-524 (`status()` returns a `str` where
`RunRecord.status` is an enum). Those are first-ten-minutes-of-use failures. While
Satay had no consumer they could read as cleanup; the moment it has one, they are
the launch.

## Decision

**1. The wedge is the debugger, not durability.** Lead with fork-from-a-prefix,
replay, and call-by-call run comparison, locally, with no account. Durability is the
mechanism that makes those possible, not the pitch. Nearest phrasing:
*the Python runtime for applications that have to explain themselves.*

**2. The first user is an application developer building AI features**, not a
platform team. Two consequences follow directly, and they are the roadmap:

- The ten usability cards above are **launch blockers**, not polish.
- **Collect-mode fan-out moves onto the critical path.** "Draft N candidates, keep
  the best" is the shape of agentic work, and discarding sibling results when one
  branch fails (ADR-0020's fail-fast semantics) is unacceptable there. KAN-473 is
  already open at high priority for independent reasons; this ADR makes it
  load-bearing rather than speculative.

**3. Platform capabilities are deferred, not cancelled.** The PostgreSQL `Store`
backend, multi-worker leasing and distributed execution keep their ARCHITECTURE §9
ordering behind the `Store` and `TaskExecutor` seams, and they come **after** the
agents-first launch. An app developer can adopt Satay today; a platform team cannot
until Postgres and multi-worker exist, so leading with platform would defer the
first user by quarters.

**4. The no-agent-abstraction non-goal holds.** Satay ships the five durable
primitives and nothing agent-shaped: no loop framework, no tool-call primitives, no
provider adapters, no graph DSL, no LangChain-scale integration breadth. The durable
agent loop is taught through examples and the cookbook, not shipped as code. If
users hand-roll the same loop three times, promote it **then**.

**5. The vendor-dossier reference app is cut.** sibei-flow's repair worker is the
reference consumer instead: it is real, it has users, and it exercises retries, side
effects, human approval and cost tracking under actual load. This also releases
KAN-403's gate, which currently blocks the `0.1.0` tag on "the reference app has
shaped the API" — a real consumer satisfies that condition better than a synthetic
one.

## Consequences

- `D-scope` in the decision register is amended: the dossier app is no longer the
  next milestone. KAN-401 and KAN-403 on board 10 are closed out accordingly.
- **The competitive claim needs care.** "Simpler than Temporal *and* better than
  LangGraph" cannot both be true, because simplicity is relative to a task and those
  tasks diverge (distributed operations versus agent authoring). The defensible
  version is a consolidation claim: *one model covers both, so you do not need two
  tools.* Do not ship the simplicity claim.
- **The debugger wedge has a funded competitor.** LangSmith is a hosted
  run-inspection product, and most readers will not distinguish it from Studio at a
  glance. The real difference — forking a genuine journal and replaying against
  recorded inputs, locally, with no account — is material but needs a **demo**
  rather than a sentence.
- ADR-0020 is now expected to be superseded by a collect-mode ADR. It stays Accepted
  until that lands.
- Nothing here changes the core-dependency boundary (ADR-0013/0016). Agents-first
  is about which usability gaps get closed, not about adding dependencies.

## Alternatives considered

- **Lead with durability** — rejected: commodity claim, crowded shelf, and the demo
  needs a paragraph of explanation where the debugger demo needs none.
- **Build agent surface to compete with LangGraph feature-for-feature** — rejected:
  it reverses the integration-breadth non-goal and trades the one differentiated
  thing (a real local debugger over a real journal) for a race against an ecosystem.
- **Platform first (Postgres, multi-worker, then launch)** — rejected: it defers the
  first user by quarters and builds against a buyer nobody has spoken to.
- **Keep the dossier app as the reference consumer** — rejected: a synthetic app
  with no users is a weaker forcing function than a real one, and it costs weeks.
