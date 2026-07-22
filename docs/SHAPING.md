---
shaping: true
---

# Satay Runtime — Shaping

Working document for requirements (R), the shape-of-record (A), the confirming
fit check (R × A), and the detailed parts. The architecture was decided upstream
in `docs/adr/0001–0010`, so this uses a **single shape-of-record** rather than a
multi-shape bake-off. Scope = MVP (see `docs/PRD.md`, decision D-scope).

---

## Requirements (R)

Top-level requirements are chunked (≤9); sub-requirements add detail.

| ID | Requirement | Status |
|----|-------------|--------|
| **R0** | **Ordinary async Python becomes durable without a framework-specific programming model** | Core goal |
| R0.1 | `@satay.workflow` (deterministic orchestration) and `@satay.task` (nondeterministic work) are the only durable boundaries authors declare | Must-have |
| R0.2 | Native `if`/`for`/`while`/`try` and function composition work inside workflows; no graph DSL | Must-have |
| R0.3 | Ordinary values pass in/out (dict, list, dataclass, TypedDict, Pydantic, enum); no mandatory framework message/state classes | Must-have |
| R0.4 | No mandatory shared mutable state object; state is reconstructed from history | Must-have |
| **R1** | **Five durable primitives, and nothing more in the MVP** | Core goal |
| R1.1 | Task: `await a_task(...)` returns a recorded, reusable result | Must-have |
| R1.2 | Durable sleep: `satay.sleep(timedelta)` survives a crash without a live process | Must-have |
| R1.3 | External event wait: `satay.wait_for_event(Type, key=, timeout=)` + `satay.send_event(key=, event=)` | Must-have |
| R1.4 | Parallel map/gather: `satay.map(fn, items, key=, concurrency=)` and `satay.gather(...)` | Must-have |
| R1.5 | Child workflow: `satay.start_child(workflow, ...)` | Must-have |
| **R2** | **Crash recovery via event-sourced replay** | Core goal |
| R2.1 | Every transition persists to an append-only, immutable journal | Must-have |
| R2.2 | On resume, completed durable calls return recorded results instead of re-executing | Must-have |
| R2.3 | At-least-once physical task execution; once a result is recorded, replay reuses it | Must-have |
| R2.4 | Replay divergence raises `NondeterminismError`; dev warns + offers fork, strict hard-fails | Must-have |
| **R3** | **Honest guarantees and tools for safe side effects** | Must-have |
| R3.1 | Stable idempotency key per logical task (stable across retries, distinct across invocations) | Must-have |
| R3.2 | Configurable retries with exponential backoff + jitter (default `retries=0`) | Must-have |
| R3.3 | `effect_safety` ∈ off / warn (dev default) / strict; strict requires idempotency/compensation for retryable side-effecting tasks | Must-have |
| R3.4 | Idempotent workflow start: same `idempotency_key` returns the same logical run | Must-have |
| **R4** | **JSON-compatible durability, no implicit pickle** | Must-have |
| R4.1 | Durable boundaries are JSON-compatible (tagged datetimes/timedeltas, explicit file/binary refs); no pickle | Must-have |
| R4.2 | Stored results rehydrate to their declared return type (Pydantic/dataclass), fall back to dict when unannotated | Must-have |
| R4.3 | Large payloads spill to a blob store (local files in dev) with a reference in the journal | Nice-to-have |
| **R5** | **Local-first runtime** | Core goal |
| R5.1 | `satay dev` starts one process: asyncio worker + SQLite store + control API + Studio | Must-have |
| R5.2 | Async-only, single-process asyncio worker; task execution passes through a `TaskExecutor` seam (`LocalTaskExecutor`) | Must-have |
| R5.3 | SQLite is the default dev backend behind a persistence interface (PostgreSQL post-MVP) | Must-have |
| **R6** | **Programmatic + external control surface** | Must-have |
| R6.1 | `satay.start(workflow, input, idempotency_key=)` returns a run handle with `result()` / `status()` / `cancel()` | Must-have |
| R6.2 | HTTP control API (`start`/`status`/`cancel`/`send_event`) writes to the store; the worker polls for events and due timers | Must-have |
| R6.3 | `TaskContext` (`ctx`) exposes the idempotency key and `record_model_usage(...)` | Must-have |
| **R7** | **Local transparency (Satay Studio)** | Core goal |
| R7.1 | Timeline view of a run (tasks, waits, timers, events, interruption) | Must-have |
| R7.2 | Execution-tree view with parent/child relationships (child workflows, map items) | Must-have |
| R7.3 | Task detail: logical task vs physical attempts, inputs/outputs, native stack traces, retry reason/delay, duration, recorded model/token/cost | Must-have |
| R7.4 | A worker interruption is visible in the timeline, showing where the crash happened and what resumed | Must-have |
| R7.5 | Fork a run from an earlier point; compare two runs | Nice-to-have |
| R7.6 | Secret / sensitive-field redaction in Studio | Must-have |
| **R8** | **Versioning and portability** | Must-have |
| R8.1 | Each run records a code version (git commit → dev-provided string → source hash) | Must-have |
| R8.2 | Version mismatch on resume warns (dev) / is rejectable (strict); user may fork | Must-have |
| R8.3 | Core is provider-agnostic — no bundled model adapters | Must-have |

---

## Shape A (shape-of-record): The ADR-decided durable runtime

The one architecture, its parts traced to the ADRs. Parts are vertical slices
(mechanism + the data it owns). No flagged unknowns (⚠️) — the mechanisms are
decided; remaining schema-level detail is spec work, noted at the end.

| Part | Mechanism | Flag | Traces to |
|------|-----------|:----:|-----------|
| **A1** | **Public API & decorators** | | ADR-0007, CONTEXT |
| A1.1 | `@satay.workflow` / `@satay.task(retries=, timeout=, side_effect=)` register definitions and wrap calls so they route through the replay engine (A2) | | ADR-0001/0007 |
| A1.2 | `satay.start(wf, input, idempotency_key=)` creates a run + returns a run handle (`result`/`status`/`cancel`) | | ADR-0006 |
| A1.3 | `TaskContext` injected into tasks: `ctx.idempotency_key`, `ctx.record_model_usage(...)` | | ADR-0006/0008 |
| **A2** | **Replay engine** | | ADR-0001/0002/0003 |
| A2.1 | Durable-call interception: each awaited durable call checks the journal; hit → return recorded result, miss → schedule/execute + append | | ADR-0001 |
| A2.2 | Identity resolver: sequential call-site ordinal + task name; `map`/`gather` items keyed by explicit `key=` | | ADR-0002 |
| A2.3 | Nondeterminism check: replayed call not matching journal → `NondeterminismError`; dev warn+fork / strict fail | | ADR-0003 |
| **A3** | **Journal & persistence** | | ADR-0004/0005 |
| A3.1 | Append-only event store (WorkflowCreated…RunForked) with atomic append; the single source of durable state | | ADR-0004 |
| A3.2 | JSON codec: tagged datetimes/timedeltas, enums, file/binary refs; no pickle | | ADR-0005 |
| A3.3 | Typed rehydration from the task's return annotation (Pydantic `model_validate` / dataclass), dict fallback | | ADR-0005 |
| A3.4 | Payload inlining ≤ ~256 KB; larger spill to a blob store (local files in dev) with a journal reference | | ADR-0004 |
| A3.5 | Persistence interface with a `SQLiteStore` implementation | | ADR-0007, CONTEXT |
| **A4** | **Task execution** | | ADR-0006/0007 |
| A4.1 | `TaskExecutor` interface; `LocalTaskExecutor` runs tasks on the asyncio loop | | ADR-0007 |
| A4.2 | Retry/backoff: exponential + jitter (base 1s, cap ~60s); records each attempt (TaskAttemptStarted/Failed) | | ADR-0006 |
| A4.3 | Idempotency-key derivation: stable function of run + logical-call identity + task name | | ADR-0006 |
| A4.4 | At-least-once semantics: re-run a task whose completion is ambiguous; reuse once recorded | | ADR-0006 |
| **A5** | **Timers & events** | | ADR-0007 |
| A5.1 | `satay.sleep` / event-wait timeouts persist as timer rows; worker poll loop (~1s dev) fires due timers | | ADR-0007 |
| A5.2 | `wait_for_event` records an EventWaitStarted; delivered events (ExternalEventReceived) resume the run | | ADR-0004 |
| **A6** | **Composite primitives** | | ADR-0001/0002 |
| A6.1 | `satay.map` / `satay.gather`: fan out durable calls with per-item `key=`, rejoin results, respect `concurrency` | | ADR-0002 |
| A6.2 | `satay.start_child`: child run linked to parent in the journal/tree | | ADR-0004 |
| **A7** | **Control API** | | ADR-0009 |
| A7.1 | Local HTTP endpoints `start`/`status`/`cancel`/`send_event` that write to the store; worker polls | | ADR-0009 |
| **A8** | **Satay Studio** | | ADR-0009 |
| A8.1 | JSON read API over the journal: run list, timeline, tree, task/attempt detail | | ADR-0009 |
| A8.2 | Web frontend: timeline, execution tree, logical-vs-attempt detail with stack traces & usage | | ADR-0009 |
| A8.3 | Fork controls + run comparison | | ADR-0004 |
| A8.4 | Secret/sensitive-field redaction on read | | ADR-0009 |
| **A9** | **CLI `satay dev`** | | ADR-0007/0009 |
| A9.1 | One process orchestrating worker + SQLite + control API + Studio | | ADR-0007 |
| **A10** | **Versioning & effect safety** | | ADR-0006/0010 |
| A10.1 | Per-run code version (git commit → dev string → source hash); mismatch warns (dev) / rejects (strict) / fork | | ADR-0010 |
| A10.2 | `effect_safety` enforcement: warn/strict checks on retryable `side_effect=True` tasks | | ADR-0006 |

---

## Fit Check (R × A)

Single confirming fit check for the shape-of-record. Binary ✅/❌.

| Req | Requirement | Status | A |
|-----|-------------|--------|:-:|
| R0.1 | `@workflow`/`@task` are the only durable boundaries authors declare | Must-have | ✅ |
| R0.2 | Native control flow inside workflows; no graph DSL | Must-have | ✅ |
| R0.3 | Ordinary values in/out; no mandatory framework classes | Must-have | ✅ |
| R0.4 | No mandatory shared mutable state object | Must-have | ✅ |
| R1.1 | Task returns a recorded, reusable result | Must-have | ✅ |
| R1.2 | Durable sleep survives a crash | Must-have | ✅ |
| R1.3 | External event wait + send_event | Must-have | ✅ |
| R1.4 | Parallel map/gather with explicit key | Must-have | ✅ |
| R1.5 | Child workflow | Must-have | ✅ |
| R2.1 | Every transition persists to an append-only journal | Must-have | ✅ |
| R2.2 | Completed durable calls return recorded results on resume | Must-have | ✅ |
| R2.3 | At-least-once physical; reuse once recorded | Must-have | ✅ |
| R2.4 | Divergence raises `NondeterminismError` (dev warn+fork / strict fail) | Must-have | ✅ |
| R3.1 | Stable idempotency key per logical task | Must-have | ✅ |
| R3.2 | Retries with exponential backoff + jitter | Must-have | ✅ |
| R3.3 | `effect_safety` off/warn/strict | Must-have | ✅ |
| R3.4 | Idempotent workflow start | Must-have | ✅ |
| R4.1 | JSON-compatible boundaries, no pickle | Must-have | ✅ |
| R4.2 | Typed rehydration via return annotation | Must-have | ✅ |
| R4.3 | Large payloads spill to blob refs | Nice-to-have | ✅ |
| R5.1 | `satay dev` one process: worker + SQLite + API + Studio | Must-have | ✅ |
| R5.2 | Async-only single-process asyncio worker behind `TaskExecutor` | Must-have | ✅ |
| R5.3 | SQLite default behind a persistence interface | Must-have | ✅ |
| R6.1 | `satay.start` + run handle (result/status/cancel) | Must-have | ✅ |
| R6.2 | HTTP control API writing to store; worker polls | Must-have | ✅ |
| R6.3 | `TaskContext` (idempotency key, record_model_usage) | Must-have | ✅ |
| R7.1 | Timeline view | Must-have | ✅ |
| R7.2 | Execution-tree view (parent/child, map items) | Must-have | ✅ |
| R7.3 | Logical-vs-physical attempts, I/O, stack traces, retry, duration, usage | Must-have | ✅ |
| R7.4 | Interruption visible in timeline | Must-have | ✅ |
| R7.5 | Fork + run comparison | Nice-to-have | ✅ |
| R7.6 | Secret redaction | Must-have | ✅ |
| R8.1 | Per-run code version | Must-have | ✅ |
| R8.2 | Mismatch warn (dev) / reject (strict) / fork | Must-have | ✅ |
| R8.3 | Provider-agnostic core, no bundled adapters | Must-have | ✅ |

**Notes:**
- Full pass — Shape A is by construction the architecture the ADRs decided to
  satisfy these requirements. Delivering parts: R0→A1; R1→A1/A5/A6; R2→A2/A3;
  R3→A4/A10; R4→A3; R5→A3.5/A4.1/A9; R6→A1/A7; R7→A8; R8→A10.
- **Detailed-design items deferred to specs** (not flagged unknowns — the
  mechanism is decided, the schema/tuning is not): exact journal event fields &
  atomic transaction boundaries (A3.1); idempotency-key derivation formula
  (A4.3); the ambiguous-completion detection rule for at-least-once (A4.4);
  testing utilities / DI / progress-streaming API shape. These are picked up by
  `build-plan-specs`.

---

## Selected shape

**Shape A** (the only shape). No flagged unknowns; ready to breadboard.
Next: `BREADBOARD.md` (affordances + wiring), then `SLICES.md` (vertical slices).
