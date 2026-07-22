---
shaping: true
slice: V2
---

# Satay Runtime — SLICE V2: Guarantees: retries, at-least-once, idempotency, nondeterminism

Turns the durable core (V1) into one with honest execution guarantees (ADR-0006)
and loud failure on divergence (ADR-0003). This slice makes tasks retry, makes
ambiguous completions safe through at-least-once plus stable idempotency keys,
upgrades V1's lightweight determinism guard into the real `NondeterminismError`,
and adds the `effect_safety` policy. Affordance IDs reference `BREADBOARD.md`;
decisions trace to `docs/adr/*.md`.

---

## Affordances

Carried over from `SLICES.md`, expanded:

| ID | Affordance | Scope in V2 |
|----|------------|-------------|
| N10 | `LocalTaskExecutor` retries with exponential backoff and jitter; records `TaskAttemptStarted`/`TaskAttemptFailed` | Full, upgrading V1's single-attempt executor |
| N4 | At-least-once re-run on ambiguous completion | Detection rule plus re-run on resume |
| N13 | Idempotency-key derivation (per-logical-task plus workflow-start key) | Full: resolves the deferred formula and enables keyed idempotent `satay.start` (PRD story 15) |
| N14 | `TaskContext`: `ctx.idempotency_key` and `ctx.record_model_usage(...)` into a generic journal usage slot | Full: the usage slot is recorded here, displayed in V6 |
| N9 | Nondeterminism check to `NondeterminismError` (expected versus actual); dev warns and offers fork, strict fails | Full, upgrading V1's guard |
| A10.2 | `effect_safety` project mode `off`/`warn` (dev)/`strict` | Full |

**Deferred or unchanged:** timers and events (N5/N11 → V3), map/gather/child
(→ V4), the control and read API (→ V5), Studio display of attempts and usage
(→ V6), and version-*mismatch* policy (N17 → V7). The V1 code-version stamp is
unchanged here.

---

## Detailed-design items resolved in this slice

V2 pins down the two guarantee-related items `SHAPING.md` and `PRD.md` deferred to
specs (A4.3, A4.4).

1. **Idempotency-key derivation formula (A4.3, ADR-0006).** A stable function of run
   identity plus logical-call identity, deliberately excluding task arguments so it
   is **stable across retries** and **distinct across invocations**:
   `key = hash(run_id, task_name, ordinal_or_map_key)`. It does not change between
   physical attempts of the same logical task, and it differs for every distinct
   durable call. It is exposed read-only via `ctx.idempotency_key`. The separate
   **workflow-start key** is the caller-supplied `idempotency_key=` on `satay.start`,
   stored on the run so repeated starts with the same key resolve to the same run
   (build step 5).

2. **Ambiguous-completion detection rule (A4.4, ADR-0006).** On resume, a logical
   task is **ambiguous** when the journal holds a `TaskAttemptStarted` (or
   `TaskScheduled`) for its identity with no subsequent `TaskCompleted` and no
   terminal retries-exhausted `TaskAttemptFailed`. Ambiguous means the attempt's fate
   is unknown, since a crash could have happened after an external effect but before
   the result was recorded, so the task re-runs (at-least-once). A logical task with
   a recorded `TaskCompleted` is never re-run (once-recorded logical completion).
   This is the rule the crash-recovery guarantee rests on, and the reason task
   authors must tolerate at-least-once and use `ctx.idempotency_key`.

---

## Build Plan

1. **Extend the journal event subset.** Activate `TaskAttemptFailed` (`task_name`,
   `ordinal` or `key`, `attempt`, `error`, `next_delay`) alongside the V1
   `TaskAttemptStarted` and `TaskCompleted`. Attempts are numbered per logical task
   (1..N). Add a generic usage slot to `TaskCompleted` (or a dedicated
   `ModelUsageRecorded` event): a schemaless `usage` object.

2. **Retry loop in `LocalTaskExecutor` (N10).** Honor `@task(retries=N, timeout=…)`.
   For each attempt: append `TaskAttemptStarted`, run the task coroutine (enforcing
   `timeout`), and on success append `TaskCompleted` and stop. On failure, append
   `TaskAttemptFailed` and, if attempts remain, wait an exponential backoff with
   jitter (base 1s, cap about 60s) then retry. After the last attempt fails, append
   `WorkflowFailed`. Backoff delays go through the injected clock seam from V1, so
   tests do not wait in real time.

3. **At-least-once on resume (N4/A4.4).** Implement the ambiguous-completion rule
   above in the replay engine's journal-consult step: a hit requires a recorded
   `TaskCompleted`, and an ambiguous partial record is treated as a miss and
   re-scheduled. Distinguish "clean, not yet started" from "ambiguous, in flight" so
   attempt numbering stays correct.

4. **Idempotency-key derivation (N13).** Implement the formula from the design
   section as a pure function over run and call identity. Compute it when a durable
   call is scheduled and thread it to the executor and the `TaskContext`.

5. **Keyed idempotent `satay.start` (N13, PRD story 15).** When `satay.start` is
   given `idempotency_key=`, look up an existing run by that key (an index on
   `runs.idempotency_key`). If found, return a handle to the same run (resume it or
   return its result) instead of creating a duplicate; if not, create the run and
   record the key. This is the keyed look-up V1 explicitly deferred; V1's
   resume-by-`run_id` path still works.

6. **`TaskContext` (N14).** Inject `ctx` into task execution, exposing
   `ctx.idempotency_key` (read-only, from step 4) and `ctx.record_model_usage(model,
   input_tokens, output_tokens, **extra)`, which writes to the generic usage slot
   (step 1). The core ships no model adapters (ADR-0008), so usage is opt-in
   self-report and a task that does not report shows no usage. Studio renders this in
   V6.

7. **Nondeterminism check (N9).** Replace V1's lightweight collision guard with a
   real check: on replay, if a durable call's resolved identity or task name does not
   match the journal entry at that position, raise `NondeterminismError` carrying
   expected versus actual for a clear message. Policy follows the safety mode
   (ADR-0003): dev warns and offers to fork, strict hard-fails. `NondeterminismError`
   is a public error type.

8. **`effect_safety` policy (A10.2, ADR-0006).** Add a project-level `effect_safety`
   mode of `off`, `warn` (the dev default), or `strict`. In `strict`, a task declared
   `side_effect=True` with `retries>0` must declare an idempotency or compensation
   strategy (for example an explicit flag or parameter), else the runtime rejects it
   with a clear error at schedule time. `warn` logs, `off` is silent. Wire the mode
   through project config and `satay.start`.

9. **Extend the demo and test tasks.** Add a task that fails twice then succeeds
   (three attempts), a task interrupted after its side effect (re-runs on restart,
   proven by the idempotency-key-guarded marker), an edited-workflow fixture that
   reorders calls (raises `NondeterminismError`), and an unguarded retryable
   side-effecting task (rejected under `strict`).

---

## Demo

A task that fails twice then succeeds shows three attempts in the timeline. A task
interrupted after its side effect re-runs on restart (at-least-once). A workflow
whose body was edited to reorder calls raises `NondeterminismError` (a warning in
dev, a failure in strict). `effect_safety=strict` rejects an unguarded retryable
side-effecting task.

---

## Test Plan

All behavior runs through the V1 seam: the public API against a temp `SQLiteStore`,
with the fault-injection hook and the manual clock. The manual clock pins backoff
*timing*; the seedable RNG seam (ADR-0011, H3) pins the *jitter*, so a backoff
schedule is exactly reproducible without real delay. Per ADR-0011 (H3) the
integration tier is boundary-only — E2E mirrors are dropped, keeping only the tests
that isolate the executor, the store index, and usage persistence.

### End-to-End Tests

- Attempts are recorded (`TaskAttemptStarted`/`TaskAttemptFailed`/`TaskCompleted`)
  and visible in the timeline; backoff is exponential with jitter, base 1s, cap
  about 60s, and deterministic under the manual clock and the seeded RNG seam.
- A task exceeding its `@task(timeout=)` fails the attempt and retries, and fails
  terminally once attempts are exhausted.
- When every attempt fails, the run terminates with `WorkflowFailed` carrying the
  last error (retry exhaustion).
- The idempotency key is stable across retries and distinct across invocations, and
  is readable via `ctx.idempotency_key`.
- A key-guarded side effect runs exactly once across an at-least-once re-run, proven
  by the idempotency-key-guarded marker.
- Keyed `satay.start` returns the same logical run for a repeated key, doing no
  duplicate work.
- An ambiguous-completion task re-runs on resume; a cleanly completed task does not.
- A divergent replay raises `NondeterminismError` with expected versus actual; dev
  warns (the offer-to-fork path lands in V7), strict fails.
- `effect_safety=strict` rejects an unguarded retryable side-effecting task; `warn`
  emits a `satay`-logger warning and `off` emits nothing (asserted by capturing the
  `satay` logger).
- `ctx.record_model_usage(...)` writes to the generic usage slot, and a non-reporting
  task records no usage.

### Integration Tests

- The executor records the full attempt sequence for a fail-twice-then-succeed task.
- An ambiguous partial record (`TaskAttemptStarted` with no `TaskCompleted`) is
  treated as a miss and re-scheduled on resume; a clean `TaskCompleted` is reused.
- Keyed `satay.start` resolves a repeated `idempotency_key` to the existing run via
  the store index.
- `ctx.record_model_usage` persists a usage object that the read path can retrieve.

### Unit Tests

- Idempotency-key derivation is stable across attempts and distinct across ordinals.
  (The distinct-across-map-keys case moves to V4, where map keys exist.)
- The backoff schedule is reproducible under the seeded RNG and stays within its
  exponential base and cap bounds.
- `NondeterminismError` carries an expected-versus-actual payload in its message.
- `effect_safety` mode parsing defaults to `warn` in dev and rejects unknown values.

---

## Dependencies

- **Upstream:** V1 (journal, replay engine, `TaskExecutor` seam, code-version stamp,
  fault-injection and clock seam).
- **Downstream:** V4 reuses idempotency identity for keyed fan-out items; V6 displays
  attempts and usage; V7 reuses the dev-warn / strict-reject split for version
  mismatch.
