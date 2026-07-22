# Satay Runtime — Initial Requirements (REQS)

> Raw initial-idea capture for the Satay Runtime project. This is the kickoff
> artifact for the product-planning process (`/build-plan-product`). It is
> deliberately concise: the exhaustive reference is
> [`initial_planning_summary.md`](./initial_planning_summary.md), which records
> all decisions made so far. Treat those decisions as accepted unless a serious
> contradiction surfaces during grilling.

## The idea

A **transparent, durable Python runtime for AI-enabled applications and
workflows.** Ordinary async Python code becomes durable, inspectable,
resumable, and replayable — without forcing developers into a heavy
framework-specific programming model.

Positioning:

> Write ordinary async Python. Satay records every step, survives failures, and
> shows you exactly what happened.

Satay is an application runtime that *understands* AI, not merely an agent
framework. Agents, chains, routers, and multi-agent systems are patterns built
**on top of** the runtime, not mandatory core abstractions.

## Who it's for (initial users)

- Python engineers at small AI startups
- Developers who have outgrown scripts but don't want a heavyweight orchestration platform
- Teams building AI-enabled applications and pipelines that need reliability, local inspection, and clear failure behavior

Not limited to autonomous-agent developers. Should serve document pipelines,
background jobs, long-running business processes, human approvals, model calls,
and conventional API workflows equally well.

## What it must do (core requirements)

1. **Durable ordinary Python** — `@workflow` for deterministic orchestration,
   `@task` for nondeterministic work. Native `if`/`for`/`while`/`try`,
   functions, and typed values stay ordinary Python. No graph DSL, no pipeline
   operators, no mandatory shared-state object.
2. **Five durable primitives** — task, durable sleep, external event wait,
   parallel map/gather, child workflow. Nothing more in the MVP.
3. **Append-only execution journal** — every transition recorded immutably;
   workflow state is reconstructed from the journal, not a global state object.
4. **Crash recovery via replay** — after interruption, re-execute workflow
   logic; completed task results are reused rather than re-run.
5. **Honest execution guarantees** — no universal exactly-once claim.
   At-least-once physical task execution, once-recorded logical completion.
   Stable idempotency keys provided for external side effects.
6. **JSON-compatible serialization** — primitives, lists, dicts, dataclasses,
   TypedDicts, Pydantic models, enums, tagged datetimes; explicit file/binary
   references. No implicit `pickle`.
7. **Local-first development** — `satay dev` runs one worker + SQLite + a local
   control API + the local debugger, no hosted paid service required.
8. **Local debugger (Satay Studio)** — timeline, execution tree, logical tasks
   vs physical attempts, native stack traces, inputs/outputs, retry reasons,
   model/token/cost metadata, run comparison, replay and fork controls,
   secret redaction.
9. **Fork, don't rewrite** — history is immutable; re-running from a point is a
   fork that leaves the original run intact.
10. **Code-version awareness** — every run records a code version; resuming
    under a changed version warns (dev) or can reject (strict). No automatic
    migration.

## First reference application

A **human-reviewed vendor research & due-diligence dossier workflow**:
validate → plan → parallel document/web research → extract claims → find gaps →
re-research as needed → draft dossier → critic/validate → pause for human
review → revise/approve → export.

Chosen because it exercises sequential + parallel + dynamic + looping +
conditional work, LLM calls, external tools, checkpointing, long-running
execution, human approval, retries, crash recovery, and replay/forking — with
lower-risk side effects than payments or account changes.

Signature demo: kill a worker mid parallel-research, restart, and show that
Satay preserved completed work, retried only unresolved work, surfaced the
interruption in the timeline, and continued to the approval step.

Likely second reference app: a document intake & decision pipeline
(upload → extract → classify → validate → human correction → persist).

## MVP scope — first vertical slice

Prove the core runtime property before anything else:

> Run a workflow of two tasks, persist every transition to SQLite, kill the
> process after the first task completes, restart, reuse the first result,
> execute the second task, and show the full execution timeline locally.

Success criteria: durable workflow creation, stable run ID, journal
persistence, task scheduling & completion persistence, process interruption,
replay with reuse of completed results, completion of remaining work,
queryable timeline, native error visibility.

## Non-goals (first MVP)

- LangChain-scale integration ecosystem
- Graph-building DSL
- General-purpose agent abstraction
- Distributed multi-region execution
- Universal exactly-once side effects
- Automatic migration of long-running workflows
- Full compensation / Saga orchestration
- Hosted commercial infrastructure, enterprise access controls, large-scale evals
- TypeScript parity
- Automatic instrumentation of arbitrary Python calls
- Implicit persistence through pickle

## Guiding principles

Ordinary Python first · explicit durable boundaries · honest guarantees ·
append-only history · native errors & stack traces · local-first · JSON-compatible
durable data · no mandatory shared state · minimal primitives · portability over
lock-in · AI-aware but not agent-only · build the runtime before the ecosystem.

## Open framing choices (to resolve during grilling)

- Language/stack for the local debugger UI (web app vs TUI vs both)
- Persistence backend ordering: SQLite (MVP) → PostgreSQL (first production)
- Public API surface finalization (`satay.start`, run handles, `result()`,
  `status()`, `cancel()`, task context, testing utilities, DI, progress streaming)
- Name confirmation: **Satay** (package/CLI `satay`, debugger "Satay Studio")
  still pending package/domain/repo/trademark checks

> Naming, guarantees, and scope are provisional — nothing here is set in stone.
