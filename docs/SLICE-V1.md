---
shaping: true
slice: V1
---

# Satay Runtime — SLICE V1: Durable two-task core with text timeline

The headline proof (PRD stories 35-36, §22). This slice establishes event-sourced
replay, the append-only journal, SQLite persistence, JSON serialization with typed
rehydration, and the primary test seam (fault injection plus deterministic time).
Everything in V2 through V8 layers on the journal and seam built here. See
`SLICES.md` for sequencing; affordance IDs (N#/U#) reference `BREADBOARD.md`;
decisions trace to `docs/adr/*.md`.

---

## Affordances

Carried over from `SLICES.md`, expanded with breadboard wiring:

| ID | Affordance | Scope in V1 |
|----|------------|-------------|
| N1 | `@satay.workflow` decorator: registers a workflow definition, wraps the call to drive replay | Full (single top-level workflow; no `start_child` yet) |
| N2 | `@satay.task(retries=, timeout=, side_effect=)`: registers a task, wraps calls as durable calls | Params accepted and stored, but **single attempt only** (no retry loop; `timeout`/`side_effect` inert until V2) |
| N3 | `satay.start(wf, input, idempotency_key=)`: create or look up a run, return a run handle | Create a run and **resume an incomplete run**; keyed idempotent look-up (N13) is deferred to V2 |
| N4 | Run handle: `result()` / `status()` | `result()` awaits the terminal state; `status()` reads current state. `cancel()` is deferred to V5 |
| N6 | Replay engine: re-runs the workflow, intercepts durable calls, matches the journal by ordinal | Full for sequential task calls |
| N7 | Identity resolver: call-site ordinal plus task name | Ordinal plus name only (`key=` fan-out is V4) |
| N8 | Journal: append-only event log, atomic append, the source of durable state | Full, for the V1 event subset |
| N10 | `LocalTaskExecutor`: runs a task on the asyncio loop | **Single attempt**; retries, backoff, and attempt history are V2 |
| N12 | JSON codec: tagged datetimes/enums/refs, no pickle, typed rehydration via return annotation | Full |
| N17 | Code-version stamper (git, then dev string, then source hash) | **Stamp only**: recorded per run; mismatch *policy* is V7 |
| CLI read | `satay runs show <id>`: print the timeline as text | Full for the V1 event subset |

**Explicitly deferred out of V1**, named here so the boundary stays honest:
retries and attempts (N10 → V2), idempotency keys (N13 → V2), nondeterminism
enforcement (N9 → V2, though see the guard note in the build plan), map/gather/child
(N5/N6 → V4), timers and events (N5/N11 → V3), the HTTP control and read API
(N15/N16 → V5), Studio (U2-U8 → V6), version-mismatch policy (N17 policy → V7),
the `satay dev` orchestrator (N20 → V8), and blob spill (N19 → V8).

---

## Detailed-design items resolved in this slice

`SHAPING.md` and `PRD.md` flag four deferred design items for `build-plan-specs`.
V1 is where the first three are pinned down; the fourth, the idempotency-key
formula, lands in V2.

1. **Journal event schema and envelope (A3.1, ADR-0004).** Every event shares an
   envelope; the payload is per-type. The V1 subset is a strict prefix of the full
   ADR-0004 set.

   - **Envelope:** `run_id`, `seq` (per-run monotonic integer, 1-based, assigned
     atomically on append), `event_id` (globally unique), `type`, `ts` (tagged UTC
     datetime), `payload`.
   - **V1 event types:** `WorkflowCreated` (`workflow_name`, `input_ref`,
     `code_version`, optional `idempotency_key`), `WorkflowResumed` (recorded when a
     worker re-drives a run that was **not** durably parked — one interrupted
     mid-execution by a crash; its presence is what renders the ⚡ interruption
     marker, ADR-0009/Q52), `TaskScheduled` (`task_name`, `ordinal`, `input_ref`),
     `TaskAttemptStarted` (`task_name`, `ordinal`, `attempt=1`), `TaskCompleted`
     (`task_name`, `ordinal`, `output_ref`), `WorkflowCompleted` (`output_ref`),
     `WorkflowFailed` (`error`: type, message, native traceback string).
   - **Ordering guarantee:** total order per run by `seq`, which is the replay and
     timeline ordering key.

2. **Transaction boundaries (A3.1).** One event is one SQLite transaction. `seq` is
   allocated inside that transaction (`MAX(seq)+1` for the run under a per-run async
   writer lock; single-process, single-writer per ADR-0007 makes this sufficient).
   A task's logical completion is a single atomic `TaskCompleted` append, and that
   is the durability commit point the crash-recovery proof hinges on.

3. **Serialization and typed rehydration (A3.2/A3.3, ADR-0005).** A JSON-compatible
   codec: primitives, lists, and dicts pass through; datetimes and timedeltas use
   tagged objects (`{"$satay": "datetime", "v": "…"}`); enums encode by value plus
   type tag; dataclasses and Pydantic models encode by field dict. No pickle. On
   replay a stored result is rehydrated from the task's return annotation (Pydantic
   `model_validate`, dataclass reconstruction), falling back to the decoded JSON
   value (a dict) when unannotated. Payloads are inlined in V1 (blob spill is V8),
   but stored behind an `input_ref`/`output_ref` indirection so V8 can swap in
   references without a schema change.

---

## Build Plan

Concrete steps, ordered so each produces something the next depends on. No code
yet; this is the plan.

1. **Package scaffold.** Create the `satay` package (Python 3.12+, Apache-2.0,
   `pyproject.toml`). Dependencies: `pydantic` for typed rehydration, the stdlib
   `sqlite3` driver wrapped for async use (or `aiosqlite`), and a CLI library
   (`typer` or `click`) for `satay runs show`. Re-export `workflow`, `task`, and
   `start` from the top-level `satay` namespace. Lay out the modules from PRD
   §Modules (`api/`, `replay/`, `journal/`, `executor/`, `codec/`, `cli/`).

2. **JSON codec (N12).** Implement encode and decode with the tagged
   representations above plus annotation-driven rehydration: `encode(value) -> json`,
   `decode(json) -> value`, `rehydrate(json, annotation) -> typed value`. Enforce
   the no-pickle invariant by rejecting un-encodable types with a clear error naming
   the offending path. This is standalone and testable first.

3. **Journal event model and persistence interface (N8).** Define the envelope and
   the V1 payloads as typed objects. Define the abstract `Store` interface:
   `create_run(...)`, `append(run_id, event) -> seq` (atomic, allocates `seq`),
   `read_events(run_id) -> list[event]` (ordered by `seq`), `get_run(run_id)`, and
   `list_runs()`. Keep it backend-agnostic, since PostgreSQL is a post-MVP impl
   behind the same interface.

4. **`SQLiteStore` implementation (A3.5).** Schema: a `runs` table (`run_id`,
   `workflow_name`, `status`, `code_version`, `created_at`, optional
   `idempotency_key`) and an append-only `events` table (`run_id`, `seq`,
   `event_id`, `type`, `ts`, `payload_json`, primary key `(run_id, seq)`). Implement
   atomic append with per-run `seq` allocation under an async writer lock. Support
   opening a temp or `:memory:` database, the substrate the test seam needs.

5. **Decorators and registry (N1/N2).** `@satay.workflow` registers a definition
   (name to callable) and returns a wrapper that, when driven by the runtime, routes
   through the replay engine. `@satay.task(retries=0, timeout=None,
   side_effect=False)` registers a task and returns a wrapper whose call inside a
   workflow becomes a durable call rather than executing inline, handing control to
   the replay engine. Store `retries`, `timeout`, and `side_effect` on the task
   definition (recorded, unused in V1). A name-keyed registry backs both replay
   matching and code-version hashing.

6. **Identity resolver (N7).** Per run-drive, keep a per-`task_name` ordinal counter
   incremented on each durable call seen during replay. The pair `(task_name,
   ordinal)` is the durable-call identity used to match journal entries. Explicit
   `key=` for fan-out is V4.

7. **Code-version stamper (N17, stamp-only).** Resolve the version once per run at
   creation: a git commit if available, else a developer-provided string, else a
   source hash of the registered definitions. Record it on `WorkflowCreated`. No
   mismatch check in V1 (that policy is V7), but store it so V7 has the data.

8. **`LocalTaskExecutor` (N10, single attempt).** Given a scheduled durable call,
   append `TaskAttemptStarted`, run the task coroutine on the loop, and on success
   append `TaskCompleted` with the encoded output. On a task exception, append
   `WorkflowFailed` (no retry in V1). It executes behind the `TaskExecutor`
   interface from day one, so V2 can add retry and backoff without touching the
   replay engine.

9. **Replay engine (N6).** The core driver. Given a `run_id`:
   - Load the ordered journal via `read_events`.
   - Re-run the workflow coroutine top-to-bottom. On each awaited durable call,
     resolve identity (step 6) and consult the journal. A **hit** (a `TaskCompleted`
     exists for this identity) returns the rehydrated recorded result **without
     executing** the task. A **miss** appends `TaskScheduled`, invokes the executor
     (step 8), and continues with the fresh result.
   - On workflow return, append `WorkflowCompleted` with the encoded output.
   - **Determinism guard (lightweight):** if a durable call's `(task_name, ordinal)`
     collides with a *different* task name already recorded at that position, raise
     a plain error for now. The full `NondeterminismError` semantics are V2; this V1
     guard just prevents a silent mis-resume.

10. **`satay.start` and the run handle (N3/N4).** `satay.start(wf, input,
    idempotency_key=None)`:
    - **New run:** allocate a stable `run_id`, append `WorkflowCreated` (with the
      stamped code version and encoded input), and begin driving via the replay
      engine.
    - **Resume path (the V1 mechanism):** if invoked against a `run_id` whose
      journal exists and is non-terminal, append `WorkflowResumed` and re-drive.
      This is how the crash-recovery demo restarts. It is **not** keyed idempotent
      look-up, which is N13 in V2. Document that boundary explicitly.
    - Return a run handle exposing `run_id`, `await result()` (drives to a terminal
      state and returns the rehydrated output, or raises the recorded failure), and
      `status()` (reads current run state from the store).

11. **Fault-injection hook and deterministic-time seam (ADR-0011).** Expose two
    first-class test affordances, not monkeypatching:
    - A **crash hook** configurable to raise or abort **after** a chosen journal
      event is committed (for example, after `TaskCompleted` for `step_one`),
      simulating a worker dying at a precise point. It fires post-commit, so the
      event is durably present when the process restarts.
    - A **clock and timer control** injected into the runtime (a real clock by
      default, a manual clock for tests). V1 behavior does not need it, but
      establishing it now means V3's timers slot into the same seam.

    Both are wired through `satay.start` and worker construction, so every future
    behavior test drives real workflows against a temp `SQLiteStore` through this
    seam.

12. **CLI read: `satay runs show <id>`.** A read-only command that opens the store,
    loads the run's ordered journal, and prints a text timeline: one line per event
    (`seq`, `ts`, `type`, key payload fields), with a ⚡ interruption and resume
    marker wherever a `WorkflowResumed` event appears (the worker writes it only on
    recovery from an interruption, so its presence is the marker; ADR-0009/Q52).
    Native task failures print with their recorded traceback. There is no `satay dev` yet; running a workflow
    in V1 is done from a Python demo script or the test harness.

13. **Wire the two-task demo.** A `demo(value)` workflow that awaits `step_one` then
    `step_two`, each an `@satay.task`. Each demo task bumps an execution-count marker
    (a file or counter) on real execution, so "reused" versus "re-executed" is
    observable (ADR-0011). Provide a script that runs it end to end and a companion
    that resumes after an injected crash, the artifacts the demo and the headline
    test drive.

---

## Demo

Run the two-task `demo(value)` workflow; kill the process after `TaskCompleted` for
`step_one`; restart; `step_one`'s result is **reused** (verified by the
execution-count marker, which shows it ran exactly once), `step_two` executes, and
the final result is correct. `satay runs show <id>` prints the full timeline
including the interruption and the resume.

---

## Test Plan

The primary test seam is the public API against a temp `SQLiteStore`, with the
fault-injection hook and manual clock injected (ADR-0011). Reuse-versus-execution is
proven with an execution-count marker, since "reused" is invisible from outputs
alone.

Per ADR-0011 (H3), the E2E tier is the primary coverage; the integration tier is
narrowed to component-boundary tests that the public-API E2E cannot reach, and
"reused vs re-executed" is asserted through the execution-count marker and the
journal, never by spying on whether the executor was invoked.

### End-to-End Tests

These are the acceptance criteria for the slice, exercised through the seam.

- Durable run creation returns a stable run ID, and `WorkflowCreated` records the
  input and the stamped code version.
- Every transition is persisted to SQLite as an ordered, append-only journal (atomic
  per-event append, per-run monotonic `seq`).
- On restart after a crash *after* `TaskCompleted` for the first task, that task's
  result is reused on replay rather than re-executed (proven by the execution-count
  marker), and the remaining task completes.
- On restart after a crash *before* the first `TaskCompleted` (killed after
  `TaskScheduled`/`TaskAttemptStarted`), the task is a journal miss and re-runs on
  resume (the marker shows it ran again), and the workflow completes. This is the
  plain miss-→-re-run case V1 owns, the un-formalised precursor of V2's ambiguous rule.
- The final workflow result is correct and returned through the run handle.
- On resume from a crash, `WorkflowResumed` is appended — the event the ⚡ interruption
  marker is computed from; the worker writes it only for a non-parked (interrupted)
  resume (ADR-0009/Q52).
- The timeline is queryable via `satay runs show <id>`, and the interruption and
  resume are visible in it.
- A native task error is recorded on `WorkflowFailed` with its traceback and
  surfaces through both the run handle and the CLI.
- `satay.start` against an already-terminal `run_id` is a no-op that returns the
  recorded result without re-driving.
- The fault-injection hook and temp-SQLite seam work as first-class affordances, and
  the headline crash-recovery test runs through them.
- Typed rehydration returns an annotated result as its declared type, an unannotated
  one as a dict, with no pickle anywhere.

### Integration Tests

- `SQLiteStore.append` allocates a monotonic `seq` across sequential appends for one
  run, and keeps per-run `seq` isolation across interleaved runs. (There are never
  concurrent appends to one run: the single worker is the sole writer, ADR-0007/0012.)
- On a journal hit the replay engine returns the recorded result and the
  execution-count marker shows the task did not re-run (asserted via the marker and
  journal, not by spying on the executor).
- On a journal miss the engine schedules and executes the task (the marker
  increments) and `TaskCompleted` is appended.
- The V1 lightweight determinism guard raises a plain error when a durable call's
  `(task_name, ordinal)` collides with a *different* task name at that position.
- The identity resolver maps repeated calls of one task to sequential ordinals
  across a run-drive.
- The code-version stamper resolves git commit, then dev string, then source hash,
  in that fallback order.
- `satay runs show` renders an ordered timeline and marks the interruption at the
  `WorkflowResumed` resume point from a seeded journal.
- The run handle `result()` drives to a terminal state and raises a recorded failure.

### Unit Tests

- The JSON codec round-trips datetime, timedelta, and enum through their tagged forms.
- The codec rejects an un-encodable type with an error naming the field path.
- `rehydrate()` reconstructs a Pydantic model and a dataclass from a stored dict.
- `rehydrate()` falls back to the decoded dict when the return annotation is absent.
- The event envelope serializes and deserializes with all required fields present.
- The ordinal counter increments independently per task name.
- Interruption-marker detection identifies the resume point that renders the ⚡
  marker — the single shared read/view-layer computation consumed by the CLI here
  and by Studio in V6 (Q42). The marker is the presence of a `WorkflowResumed` event,
  which the worker writes only on an interrupted (non-parked) resume (Q52).

---

## Dependencies

- **Upstream:** none. V1 is the critical path and the root of the dependency graph.
- **Downstream:** V2 (guarantees), V3 (timers and events), V4 (composite), and V5
  (control and read API) all build on the journal, the `TaskExecutor` seam, and the
  test seam established here.
