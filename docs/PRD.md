# Satay Runtime — PRD (MVP)

> Synthesized 2026-07-20 from `REQS.md`, `CONTEXT.md`, `docs/adr/0001–0010`, and
> answered `QUESTIONS.md`. Uses the CONTEXT glossary vocabulary throughout and
> respects all accepted ADRs. Scope is the MVP (decision **D-scope**): the durable
> runtime + its five primitives + SQLite persistence + Satay Studio + the two-task
> crash-recovery vertical slice as the proof. The vendor-dossier showcase app is
> the **next** milestone, not part of this PRD.

## Problem Statement

A Python engineer building an AI-enabled application or pipeline needs it to
survive crashes, resume without redoing completed work, and be inspectable when
something goes wrong. Today they choose between two bad options:

- **Write it themselves** — bespoke checkpointing, retry, and idempotency logic
  smeared across the app, with no consistent execution history and painful
  debugging after a failure.
- **Adopt a heavyweight orchestration framework** — which forces their ordinary
  code into graph DSLs, message/state classes, and custom operators; hides native
  stack traces; couples them to one ecosystem; and often requires a hosted paid
  service to inspect what happened.

They want durability and transparency **without** rewriting their application
around a framework, and without giving up native async Python, native errors, or
local debugging.

## Solution

Satay is a transparent, durable Python runtime. The developer writes ordinary
async Python: a `@satay.workflow` orchestration function that composes
`@satay.task` calls with normal `if`/`for`/`while`/`try`. Satay records every
durable call to an append-only journal, so:

- If a worker crashes mid-run, restarting **replays** the workflow and reuses
  already-recorded task results instead of re-executing them.
- Everything that happened — tasks, attempts, retries, timers, events, inputs,
  outputs, model usage, native stack traces — is visible locally in **Satay
  Studio**, a web debugger launched by `satay dev`, with no hosted service.
- The developer keeps ordinary values (dicts, dataclasses, Pydantic models),
  native exceptions, and their own provider SDK calls inside tasks — no framework
  message/state/graph classes.

The MVP proves the core property end to end: run a two-task workflow, persist
every transition to SQLite, kill the process after the first task completes,
restart, reuse the first result, run the second task, and show the full timeline.

## User Stories

**Authoring the programming model**

1. As a Python developer, I want to mark a function `@satay.workflow`, so that its execution becomes durable and replayable.
2. As a Python developer, I want to mark a function `@satay.task`, so that its result is recorded and reused on replay and it is the place my I/O lives.
3. As a Python developer, I want to call one task from a workflow with a normal `await`, so that sequential composition needs no special syntax.
4. As a Python developer, I want to use ordinary `if`/`for`/`while`/`try` in a workflow, so that I don't have to learn a graph DSL.
5. As a Python developer, I want to pass and return ordinary values (dicts, lists, dataclasses, TypedDicts, Pydantic models, enums), so that I don't rewrite my types around the framework.
6. As a Python developer, I want typed task results to come back as their declared type on replay, so that my type hints keep working after recovery.
7. As a Python developer, I want to configure `retries` and a timeout per task, so that transient failures are handled without hand-rolled loops.
8. As a Python developer, I want a `TaskContext` (`ctx`) available in a task, so that I can read the stable idempotency key and record model usage.

**Durable primitives**

9. As a Python developer, I want `satay.map(fn, items, key=…, concurrency=N)`, so that I can fan out work in parallel with stable replay identity per item.
10. As a Python developer, I want `satay.gather(...)`, so that I can await several heterogeneous durable calls together.
11. As a Python developer, I want `satay.sleep(timedelta)`, so that a workflow can wait durably across a crash without holding a live process.
12. As a Python developer, I want `satay.wait_for_event(Type, key=…, timeout=…)`, so that a workflow can pause for an external decision (e.g. human approval) and resume when it arrives.
13. As a Python developer, I want `satay.start_child(workflow, ...)`, so that I can compose workflows out of sub-workflows.

**Running and controlling workflows**

14. As a Python developer, I want `satay.start(workflow, input, idempotency_key=…)`, so that I can launch a run and get a stable run ID back.
15. As a Python developer, I want repeated starts with the same idempotency key to return the same logical run, so that duplicate submissions don't create duplicate work.
16. As a Python developer, I want a run handle with `result()`, `status()`, and `cancel()`, so that I can await, poll, or stop a run.
17. As an external caller, I want to `send_event(key=…, event=…)` to a running workflow, so that I can deliver approvals or signals from outside the worker process.
18. As an operator, I want to run everything locally with `satay dev`, so that one command starts the worker, SQLite store, control API, and Studio.

**Durability, recovery, and guarantees**

19. As a Python developer, I want every transition persisted to an append-only journal, so that the run's history is complete and immutable.
20. As a Python developer, I want a crashed run to resume by replaying and reusing recorded task results, so that completed work is never wasted.
21. As a Python developer, I want a task that crashed with ambiguous completion to be retried (at-least-once), so that in-flight work isn't silently lost.
22. As a Python developer, I want a stable idempotency key per logical task, so that I can make external side effects safe with providers that support it.
23. As a Python developer, I want an `effect_safety` mode (`off`/`warn`/`strict`), so that side-effecting retryable tasks are checked appropriately in dev vs prod.
24. As a Python developer, I want a clear `NondeterminismError` when replay diverges from history, so that a non-deterministic workflow fails loudly instead of resuming wrongly.
25. As a Python developer, I want each run to record a code version, so that resuming under changed code is detected.
26. As a Python developer, I want a version mismatch to warn (dev) or be rejectable (strict), so that I decide consciously whether to resume or fork.

**Inspection and replay (Satay Studio)**

27. As a Python developer, I want a timeline view of a run, so that I can see the order of tasks, waits, timers, and events.
28. As a Python developer, I want an execution-tree view, so that I can see parent/child relationships including child workflows and map items.
29. As a Python developer, I want to distinguish a logical task from its physical attempts, so that I can see how many times something actually ran.
30. As a Python developer, I want to expand a task to see inputs, outputs, native stack traces, retry reason and delay, duration, and recorded model/token/cost usage, so that I can debug without a hosted service.
31. As a Python developer, I want a worker interruption to be visible in the timeline, so that I can see exactly where a crash happened and what resumed.
32. As a Python developer, I want to fork a run from an earlier point, so that I can re-run with a changed task, model, prompt, input, or retry policy while leaving the original intact.
33. As a Python developer, I want to compare two runs, so that I can see what a change did.
34. As a Python developer, I want secrets and sensitive fields redacted in Studio, so that inspection doesn't leak credentials.

> **MVP Studio scope (D16, ADR-0013):** the MVP ships four views — run list, timeline
> with the interruption marker, execution tree, and task/attempt detail with usage —
> plus redaction (stories 27-31, 34). **Fork (32), run-compare (33), and the
> version-mismatch banner are deferred**; all stay in the JSON API design so they are
> cheap to add later.

**The proof (vertical slice)**

35. As a Python developer, I want to run the two-task `demo` workflow, kill the process after task one, restart, and see task one's result reused and task two executed, so that I can trust the core durability property.
36. As a Python developer, I want the completed run's timeline to be queryable locally, so that I can verify what happened after the fact.

## Implementation Decisions

Traceability: each decision references the ADR (or CONTEXT decision) of record.

- **Execution model — event-sourced replay** (ADR-0001). The workflow function
  re-runs top-to-bottom on resume; each durable call consults the journal and
  returns a recorded result if present, else executes and appends. The journal is
  the only durable workflow state; there is no shared mutable state object.
- **Durable-call identity** (ADR-0002). Ordinary calls are identified by
  sequential call-site ordinal + task-definition name. `satay.map`/`gather`
  require an explicit `key=` per item. No framework identifiers in task
  signatures.
- **Nondeterminism detection** (ADR-0003). Runtime-only for the MVP; a replay
  mismatch raises `NondeterminismError` carrying expected-vs-actual; dev warns +
  offers fork, strict hard-fails.
- **Journal** (ADR-0004). Append-only, immutable, JSON-compatible event log with
  event types per summary §21B (`WorkflowCreated`, `TaskScheduled`,
  `TaskAttemptStarted`, `TaskAttemptFailed`, `TaskCompleted`, `TimerCreated`,
  `TimerFired`, `EventWaitStarted`, `ExternalEventReceived`, `WorkflowWaiting`,
  `WorkflowResumed`, `WorkflowCompleted`, `WorkflowFailed`, `WorkflowCancelled`,
  `RunForked`, …). Exact event fields, atomic transaction boundaries, ordering
  guarantees, event IDs, and any compaction are finalized in the persistence-schema
  design stage. Forking creates a new run from an earlier journal point; the
  original is never rewritten. Payloads over **262144 bytes (256 KiB encoded)** spill
  to a local blob file with the journal keeping a reference; redaction runs after
  rehydration so spilled payloads are scrubbed like inline ones (ADR-0004 H3).
- **Serialization & rehydration** (ADR-0005). JSON-compatible by default (no
  pickle); datetimes/timedeltas via tagged representations; explicit file/binary
  references. Stored results are rehydrated using the task's return type
  annotation (Pydantic `model_validate`, dataclass reconstruction), falling back
  to dict when unannotated.
- **Execution guarantees** (ADR-0006). At-least-once physical task execution with
  once-recorded logical completion. Stable idempotency key per logical task via
  `ctx.idempotency_key`. Default `retries=0`; when set, exponential backoff with
  jitter (base 1s, cap ~60s). `effect_safety` ∈ off/warn(dev)/strict; in strict, a
  retryable `side_effect=True` task must declare an idempotency/compensation
  strategy. No universal exactly-once claim; no Saga/compensation in the MVP. Backoff
  jitter draws from a **seedable RNG seam** so schedules are deterministic in tests
  (ADR-0011 H3).
- **Runtime & worker model** (ADR-0007). Async-only. Single-process asyncio worker.
  Task execution passes through a `TaskExecutor` interface; MVP impl
  `LocalTaskExecutor`. `satay.map(concurrency=N)` = asyncio concurrency in one
  process. Durable sleep and event-wait timeouts are timer rows the worker polls
  (~1s in dev); no external scheduler.
- **Model observability** (ADR-0008). Tasks self-report via
  `ctx.record_model_usage(model, input_tokens, output_tokens, …)`; the journal
  stores a generic usage/cost slot. Core ships no model adapters; the reference
  app (next milestone) will call a provider SDK directly inside tasks.
- **Local surfaces** (ADR-0009). `satay dev` runs one process: asyncio worker +
  SQLite store + HTTP control API (`start`/`status`/`cancel`/`send_event`, writes
  to store; worker polls for events/timers) + Satay Studio web app served over the
  same JSON API. TUI deferred behind the API seam.
- **Code versioning** (ADR-0010). Per-run version resolved git commit → dev string
  → source hash. Mismatch on resume warns (dev) / rejectable (strict); user may
  fork. No automatic migration.
- **Persistence backends** (CONTEXT). SQLite = default dev backend (MVP);
  PostgreSQL = first production backend (post-MVP); Redis is not the durable store.
- **API co-hosting & single writer** (ADR-0012). The control/read API runs on its own
  thread: it reads SQLite directly (WAL read-only connections) and routes writes
  through an in-process command queue to the worker, which stays the sole writer.
  SQLite is driven by a dedicated writer thread over stdlib `sqlite3`. SQLite is kept
  for the MVP; the phased roadmap (SQLite → PostgreSQL → multi-worker) rides the
  `Store`/`TaskExecutor` seams (ARCHITECTURE §9).
- **Packaging & dependency surface** (ADR-0013). Lean pure-Python core; Pydantic is
  duck-typed, not a core dependency. The debugger stack (FastAPI + uvicorn + the built
  Studio bundle) ships in a `satay[studio]` extra. Studio is Svelte + Vite + TS (plain
  SPA; d3 for timeline/tree), prebuilt in CI and vendored into the extra, never built
  at `pip install`.
- **Local-surface security** (ADR-0014). Loopback bind on a random port, a per-session
  token Studio must present, and an `Origin`/`Host` allow-list, to close CSRF /
  DNS-rebinding on the browser surface. Not networked authentication. The guard is
  enforced by the API server and negative-tested from **V5**; `satay dev` issues the
  token (V8) with a smoke test (ADR-0014 H3).
- **Toolchain** (ADR-0015). uv, hatchling, Ruff, mypy strict, pytest + pytest-asyncio,
  Vitest; code-version chain = git binary else source hash (dulwich dropped, refining
  ADR-0010).
- **Core dependency boundary** (ADR-0016). Minimal `argparse` core CLI for `satay runs
  show`; `satay dev` + Typer in the `satay[studio]` extra. Journal events are stdlib
  frozen dataclasses; database access is raw parameterized SQL over `sqlite3` (no ORM).
  `satay runs show` is frozen at the V1 event subset for the MVP; Studio covers the
  event types added later (ADR-0016 H3).
- **Persistence layout** (ADR-0017). Default data dir `./.satay/` (overridable with
  `--data-dir`); schema versioned via `PRAGMA user_version` with forward-only
  migrations; a DB written by a newer `satay` is refused.
- **Frontend & Studio delivery** (ADR-0018). Svelte 5 (runes) + Vite + pnpm, pinned
  Node LTS in CI; plain CSS + minimal routing; Studio polls the read API (no push in
  the MVP); OpenAPI but unversioned.
- **Platform, release & tooling** (ADR-0019). Linux + macOS first-class (local disk
  only), Windows best-effort; tested on Python 3.12 and 3.13; PyPI via OIDC trusted
  publishing; stdlib `logging`; hand-rolled retry; pytest-cov with optional hypothesis.
- **Composite failure semantics** (ADR-0020, superseded by ADR-0027).
  `satay.map`/`gather`/`start_child` are fail-fast **by default**: a failed item, gather
  member, or child raises through the composite like a native `await`, and a failed child
  re-raises deterministically from the journal on parent replay. `map` and `gather` also
  take `return_exceptions=True` for collect mode, which lets every member settle and
  records each collected task failure as a terminal `TaskFailed` event.
- **Event ordering & timeout race** (ADR-0021). A matching event wins over a
  simultaneously-due `wait_for_event` timeout (check event, then timeout, per poll tick);
  multiple buffered matches for one `(type, key)` are consumed FIFO by `received_at`.
- **Public API surface** (CONTEXT glossary; summary §21C). `@satay.workflow`,
  `@satay.task(retries=, timeout=, side_effect=)`, `satay.start`, run handle
  (`result`/`status`/`cancel`), `satay.sleep`, `satay.wait_for_event`,
  `satay.send_event`, `satay.map`, `satay.gather`, `satay.start_child`,
  `TaskContext`. Testing utilities, dependency injection, and progress streaming
  are finalized during shaping.
- **Packaging** (CONTEXT). Package/CLI `satay`, command `satay dev`, debugger
  "Satay Studio"; name provisional. Apache-2.0. Python 3.12+.

### Modules (greenfield, indicative — finalized in shaping/specs)

- **Public API / decorators** — `@workflow`, `@task`, module-level primitives, run
  handle, `TaskContext`. The single highest test seam.
- **Replay engine** — durable-call interception, journal matching by ordinal/key,
  nondeterminism detection.
- **Journal / persistence** — append-only event store, payload inlining/spill,
  behind a persistence interface with a SQLite implementation.
- **TaskExecutor** — `LocalTaskExecutor` (asyncio), retry/backoff, idempotency-key
  derivation, lease/heartbeat concerns.
- **Timers & events** — timer rows, event rows, worker polling.
- **Control API** — local HTTP endpoints over the store.
- **Studio** — web frontend + JSON read API (timeline, tree, attempts, fork,
  compare, redaction).
- **CLI** — core `satay runs show` (argparse); `satay dev` orchestration in the
  `satay[studio]` extra.

## Testing Decisions

**What makes a good test here:** exercise external behavior through the public API,
never private replay internals. A test drives a real workflow via `satay.start`
against a temporary SQLite store and asserts on observable outcomes — the returned
result, the run status, and the recorded journal — not on internal call sequences.

**Primary seam (greenfield — no prior art; this establishes the pattern):** the
**public API driving the runtime against a temp SQLite store, with two injected
controls**: (1) a **fault-injection hook** that can terminate/simulate a worker
crash after a chosen journal event (e.g. after `TaskCompleted` for task one), and
(2) deterministic control over time/timers so `sleep`/timeouts are testable
without real waiting. This is the single, highest seam; prefer it for all
behavior tests. Future tests follow this pattern rather than reaching into modules.
This test strategy is the decision of record in **ADR-0011**.

**Modules tested through that seam:**

- **Crash recovery (the headline test):** run the two-task `demo`, inject a crash
  after task one completes, restart, assert task one's result is reused (not
  re-executed — verified via an execution counter/side-effect marker), task two
  runs, final result is correct, and the journal shows the interruption and resume.
- **Replay reuse & identity:** completed durable calls return recorded results;
  `satay.map` items match by `key=` regardless of completion order.
- **Nondeterminism:** a divergent replay raises `NondeterminismError`; dev warns,
  strict fails.
- **Primitives:** `sleep` resumes after a durable wait; `wait_for_event` blocks
  then resumes on `send_event`; `gather`/`map` fan out and rejoin; `start_child`
  composes.
- **Guarantees:** at-least-once retry on ambiguous completion; stable idempotency
  key across retries and distinct across invocations; `effect_safety=strict`
  rejects an unguarded retryable side-effecting task.
- **Serialization:** dataclass/Pydantic/enum/datetime round-trip; typed
  rehydration via return annotation; no-pickle invariant.
- **Code version:** mismatch on resume warns (dev) / rejects (strict).

Studio is verified through its JSON read API (timeline/tree/attempt payloads,
redaction) rather than through UI rendering in the MVP.

**H3 test-audit refinements (2026-07-22; see TESTING.md).** The E2E public-API tier is
the primary coverage; the integration tier is reserved for component-boundary tests it
cannot reach, not mirrors of E2E cases (ADR-0011). Facts like reuse-vs-re-execution are
asserted through the execution-count marker and journal, never by spying on internals.
Determinism controls are the manual clock **and** a seedable RNG seam (for backoff
jitter). New coverage the audit adds: reads still return while the worker is stalled
mid-write (ADR-0012); fail-fast failure paths for `map`/`gather`/`start_child`
(ADR-0020); event-wins-over-timeout and FIFO event ordering (ADR-0021); an exact-boundary
spill test with redaction of a spilled secret (ADR-0004); and V5 security-guard negative
tests (ADR-0014).

## Out of Scope

Per REQS non-goals and D-scope:

- The vendor-dossier reference app and the document-intake pipeline (next milestones).
- LangChain-scale integration ecosystem; any bundled model adapters.
- A graph-building DSL; a general-purpose agent abstraction.
- Distributed / multi-worker / multi-region execution; PostgreSQL backend (post-MVP).
- Universal exactly-once side effects; full compensation / Saga orchestration.
- A collect flag on `start_child`; it is a single call, so `try`/`except` covers it and a
  fan-out collects it as a `gather` member (ADR-0027).
- `satay runs show` rendering of post-V1 event types; the CLI is frozen at the V1 subset
  and Studio covers the rest (ADR-0016).
- Automatic migration of long-running workflows across code versions.
- Static analysis of workflow bodies (nondeterminism is runtime-only for MVP).
- Sync (non-async) workflows/tasks.
- Hosted commercial infrastructure, enterprise access controls, large-scale evals.
- TypeScript parity; automatic instrumentation of arbitrary Python calls; implicit pickle.
- A TUI debugger (deferred behind the JSON API seam).
- Studio fork and run-compare views and the version-mismatch banner (deferred to a
  later Studio; the JSON API keeps them cheap to add).
- Windows as a first-class platform, and SQLite on network filesystems (best-effort /
  unsupported for the MVP; ADR-0019).

## Further Notes

- **Sequencing.** The recommended first build is the two-task crash-recovery slice
  (stories 35–36) before wiring the full primitive set and Studio breadth — it
  proves the hardest property earliest (summary §22).
- **Determinism rule is load-bearing.** All I/O, clocks, randomness, and external
  calls must live in tasks, never directly in workflow bodies; this underpins
  ADR-0001/0002/0003 and is the single most important thing to teach users.
- **Open detailed-design items** carried into shaping/specs: exact journal event
  schema and transaction boundaries (ADR-0004); lease/heartbeat and duplicate-attempt
  handling for `LocalTaskExecutor`; idempotency-key derivation formula; testing
  utilities / DI / progress-streaming API shape.
- **Name/packaging** remain provisional pending PyPI/domain/trademark checks.
