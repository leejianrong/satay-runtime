# Satay Runtime — CONTEXT

> Shared source of truth for the project: the **glossary** (what each term means,
> used consistently everywhere downstream) and the **decision register** (every
> load-bearing decision with its status and ADR). Produced by grilling on
> 2026-07-20 from `REQS.md` + `initial_planning_summary.md`, confirm-only mode.

---

## Glossary

Terms below are canonical. Use them exactly in the PRD, shaping docs, code, and UI.

| Term | Meaning |
|---|---|
| **Workflow** | A deterministic orchestration function decorated `@satay.workflow`. Composes durable calls with ordinary Python control flow. Contains **no** direct I/O, clocks, or randomness. |
| **Task** | A nondeterministic unit of work decorated `@satay.task`. Where all I/O, model calls, clocks, randomness, and external effects live. |
| **Durable call** | Any awaited call whose result is recorded in the journal and reused on replay: a task call, `satay.map`, `satay.gather`, `satay.sleep`, `satay.wait_for_event`, `satay.start_child`. |
| **Durable primitive** | One of the five runtime-provided durable operations: **task**, **durable sleep**, **external event wait**, **parallel map/gather**, **child workflow**. |
| **Journal** | The append-only, immutable, JSON-compatible event log for a run. The single source of durable state; the debugger timeline is a view of it. |
| **Journal event** | One recorded transition (e.g. `TaskScheduled`, `TaskCompleted`, `TimerFired`, `ExternalEventReceived`). |
| **Run** | A single execution of a workflow, identified by a stable **run ID**. |
| **Replay** | Recovery by re-executing the workflow function top-to-bottom; durable calls with a journaled result return it instead of re-executing. |
| **Logical task** | A task invocation identified within a run (by call-site ordinal, or explicit `key=` in a map). Its result is recorded once. |
| **Physical attempt** | One actual execution of a logical task. A logical task may have several attempts (retries, or ambiguous-completion re-runs). |
| **Call-site ordinal** | The sequential position of a durable call within a workflow run, combined with the task-definition name, used to match a call to its journal entry. |
| **Idempotency key** | A stable key Satay derives per logical task invocation, stable across retries and distinct across invocations, for making external effects safe. |
| **Side-effecting task** | A task declared `side_effect=True`; subject to `effect_safety` policy. |
| **effect_safety** | Project mode governing side-effect safety: `off` / `warn` (dev default) / `strict`. |
| **NondeterminismError** | Raised when a replayed durable call does not match the journal (code changed, non-deterministic branch, reordered calls). |
| **Fork** | A new run branched from an earlier point of an existing run's journal; the original is never rewritten. |
| **Code version** | The identifier recorded per run (git commit → dev-provided string → source hash) used to detect mismatch on resume. |
| **TaskExecutor** | Internal interface through which task execution passes. MVP impl: `LocalTaskExecutor`. |
| **Control API** | Local HTTP API for `start` / `status` / `cancel` / `send_event`, writing to the store. |
| **Satay Studio** | The local debugger: a web app served by `satay dev` over the JSON API. |
| **satay dev** | The single local process that runs the worker, SQLite store, control API, and Studio. |
| **Primary test seam** | The public API driving real workflows against a temp SQLite store, with injected fault-injection and deterministic-time controls; the single highest seam for behavior tests. |
| **Fault-injection hook** | A first-class test affordance that terminates / simulates a worker crash after a chosen journal event. |
| **Command queue** | The in-process queue the API thread uses to hand writes (`start`/`cancel`/`send_event`/`fork`) to the worker, which stays the sole journal writer. |
| **satay[studio] extra** | The optional install that adds the debugger stack (FastAPI + uvicorn + the built Studio bundle) on top of the pure-Python core. |
| **Interruption marker** | The timeline's ⚡ indicator that a run was interrupted mid-execution and recovered by replay. Defined as the **presence of a `WorkflowResumed` event**, which the worker appends *only* when re-driving a run that was **not** durably parked (a crash) — never on a graceful wake from a `WorkflowWaiting`. Computed once in the read/view layer, shown by both the CLI and Studio (ADR-0009; Q52 corrected the earlier `WorkflowWaiting`→`WorkflowResumed` wording). |
| **Blob spill** | Storing a payload larger than the spill threshold (262144 bytes / 256 KiB encoded) in a local blob file, with the journal keeping only a reference; resolved transparently on read and replay (ADR-0004). |

---

## Decision register

| id | decision | status | ADR |
|---|---|---|---|
| D1 | Event-sourced replay execution model (re-run workflow, reuse journaled results) | Accepted | [ADR-0001](adr/0001-event-sourced-replay.md) |
| D2 | Durable-call identity = call-site ordinal + task name; fan-out uses explicit `key=` | Accepted | [ADR-0002](adr/0002-durable-call-identity.md) |
| D3 | Runtime-only nondeterminism detection → `NondeterminismError` (dev warn+fork / strict fail) | Accepted | [ADR-0003](adr/0003-nondeterminism-detection.md) |
| D4 | Append-only immutable journal as single source of truth; fork, never rewrite | Accepted | [ADR-0004](adr/0004-append-only-journal.md) |
| D5 | JSON-compatible serialization, no pickle; typed rehydration via return annotations | Accepted | [ADR-0005](adr/0005-serialization-and-rehydration.md) |
| D6 | Execution guarantees: at-least-once physical / once-recorded logical; stable idempotency keys; `effect_safety` modes; retry defaults | Accepted | [ADR-0006](adr/0006-execution-guarantees.md) |
| D7 | Local-first single-process asyncio runtime, async-only, behind a `TaskExecutor` seam | Accepted | [ADR-0007](adr/0007-runtime-and-worker-model.md) |
| D8 | Model/token/cost via `ctx` self-report; core ships no model adapters | Accepted | [ADR-0008](adr/0008-model-observability.md) |
| D9 | Satay Studio = local web app over a JSON control/read API; events via store polling | Accepted | [ADR-0009](adr/0009-local-surfaces.md) |
| D10 | Code-version recorded per run (git → dev string → source hash); mismatch surfaced honestly | Accepted | [ADR-0010](adr/0010-code-versioning.md) |
| D11 | Primary test seam = public API + temp SQLite + fault-injection + deterministic time; assert on observable outcomes | Accepted | [ADR-0011](adr/0011-test-strategy-and-seam.md) |
| D12 | API co-hosting + single-writer model: separate API thread, reads direct, writes via command queue to the sole worker writer; SQLite via a dedicated writer thread; SQLite kept for MVP | Accepted | [ADR-0012](adr/0012-api-cohosting-and-single-writer.md) |
| D13 | Lean core + `satay[studio]` extra; Pydantic duck-typed (not core); FastAPI+uvicorn and the Svelte+Vite bundle in the extra, prebuilt in CI | Accepted | [ADR-0013](adr/0013-packaging-and-frontend-stack.md) |
| D14 | Local-surface security: loopback + random port + per-session token + `Origin`/`Host` allow-list | Accepted | [ADR-0014](adr/0014-local-surface-security.md) |
| D15 | Toolchain: uv, hatchling, Ruff, mypy strict, pytest-asyncio, Vitest; code-version = git binary else source hash (dropped dulwich) | Accepted | [ADR-0015](adr/0015-development-toolchain.md) |
| D16 | Studio MVP scope = four views (run list, timeline, execution tree, task/attempt detail); fork/compare/version-banner deferred | Accepted | [ADR-0013](adr/0013-packaging-and-frontend-stack.md), [PRD](PRD.md) |
| D17 | Core dependency boundary: minimal argparse core CLI (Typer + `satay dev` in the extra); stdlib frozen-dataclass event model; raw SQL over `sqlite3`, no ORM | Accepted | [ADR-0016](adr/0016-core-dependency-boundary.md) |
| D18 | Persistence layout: project-local `./.satay/` (`--data-dir`); forward-only schema migrations via `PRAGMA user_version` | Accepted | [ADR-0017](adr/0017-persistence-layout-and-migrations.md) |
| D19 | Frontend/Studio delivery: Svelte 5 + pnpm + pinned Node; plain CSS + minimal routing; Studio polls the read API; unversioned OpenAPI | Accepted | [ADR-0018](adr/0018-frontend-and-studio-delivery.md) |
| D20 | Platform/release/tooling: Linux+macOS first-class (local disk only), Python 3.12/3.13; PyPI via OIDC; stdlib logging; hand-rolled retry; pytest-cov + optional hypothesis | Accepted | [ADR-0019](adr/0019-platform-release-and-tooling.md) |
| D21 | Composite failure semantics: `map`/`gather`/`start_child` are fail-fast (a failed part raises through the composite, like native `await`); collect-style deferred post-MVP | Accepted | [ADR-0020](adr/0020-composite-failure-semantics.md) |
| D22 | Event delivery: a matching event wins over a simultaneously-due `wait_for_event` timeout; multiple buffered matches consumed FIFO by `received_at` | Accepted | [ADR-0021](adr/0021-event-ordering-and-timeout-race.md) |
| D-name | Product/package/CLI name **Satay**; debugger "Satay Studio" | Accepted (provisional) | — pending PyPI/domain/trademark |
| D-license | **Apache-2.0** license | Accepted | — |
| D-python | **Python 3.12+** minimum | Accepted | — |
| D-scope | MVP = runtime (5 primitives) + SQLite + Studio + two-task crash-recovery slice; dossier app is the next milestone | Accepted | — see docs/PRD.md |

### Persistence backend ordering (decided in summary, recorded here)
SQLite = default local/dev backend (MVP). PostgreSQL = first production backend
(post-MVP). Redis is **not** the durable execution store. This ordering is the
phased roadmap (SQLite → PostgreSQL → multi-worker) in ARCHITECTURE §9, carried by
the `Store` and `TaskExecutor` seams (ADR-0007, ADR-0012).
