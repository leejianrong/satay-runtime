---
shaping: true
---

# Satay Runtime — Slices

Vertical implementation increments for Shape A. Each slice is a thin end-to-end
cut that ends in **demo-able output**, sequenced along the orthogonal concerns in
`BREADBOARD.md`. Affordance IDs (N#/U#) reference that breadboard. This is the
hand-off to `build-plan-specs`.

**Sequence & dependencies:**

```
V1 (durable core + text timeline)  ← the headline proof (PRD §22)
   ├─ V2 (guarantees: retries, at-least-once, idempotency, nondeterminism)
   ├─ V3 (timers & events)
   └─ V4 (composite primitives + parallel crash-recovery)
V5 (control + read API)            ← needs V1 journal
   └─ V6 (Satay Studio web)        ← needs V5 read API
         └─ V7 (fork, compare, version mismatch)
V8 (satay dev unified stack + payload spill)   ← ties it together
```

---

## V1 — Durable two-task core with text timeline

**The headline proof.** Establishes replay, the journal, SQLite persistence, and
the primary test seam.

- **Affordances:** N1 `@workflow`, N2 `@task` (no retries yet), N3 `satay.start`,
  N4 run handle `result()`/`status()`, N6 replay engine, N7 identity (ordinal),
  N8 append-only journal, N10 `LocalTaskExecutor` (single attempt), N12 JSON codec
  + typed rehydration, N17 record code version (stamp only). Plus a minimal
  CLI read (`satay runs show <id>`) printing the timeline as text.
- **Demo:** Run the two-task `demo(value)` workflow; kill the process after
  `TaskCompleted` for `step_one`; restart; `step_one` result is **reused**
  (verified by an execution-count marker), `step_two` executes, final result is
  correct; `satay runs show` prints the full timeline including the interruption
  and resume.
- **Acceptance:** Durable run creation with a stable run ID; every transition
  persisted to SQLite; completed task result reused on replay; remaining task
  completes; timeline queryable; native errors visible. Fault-injection hook +
  temp-SQLite test seam in place (PRD testing section).

## V2 — Guarantees: retries, at-least-once, idempotency, nondeterminism

- **Affordances:** N10 retries w/ exponential backoff + jitter (records
  TaskAttemptStarted/Failed), N4 at-least-once re-run on ambiguous completion,
  N13 idempotency-key derivation, N14 `TaskContext`
  (`ctx.idempotency_key`, `ctx.record_model_usage` → generic journal usage slot;
  displayed in V6),
  N9 nondeterminism check → `NondeterminismError`, N10/A10.2 `effect_safety`
  off/warn/strict.
- **Demo:** A task that fails twice then succeeds shows 3 attempts in the
  timeline; a task interrupted after its side effect re-runs on restart; a
  workflow whose body was edited to reorder calls raises `NondeterminismError`
  (strict by default since ADR-0022; `warn`/`off` are opt-ins); `effect_safety=strict`
  rejects an unguarded retryable side-effecting task.
- **Acceptance:** Attempts recorded and shown; stable idempotency key across
  retries and distinct across invocations; divergent replay raises the typed
  error with expected-vs-actual; strict-mode enforcement works.

## V3 — Timers & events

- **Affordances:** N5 `satay.sleep`, `satay.wait_for_event`, `satay.send_event`;
  N11 timer & event loop (timer rows, ~1s poll, event delivery + resume).
- **Demo:** A workflow calls `satay.sleep(...)`, the process is idle (no live
  workflow frame), and it resumes when the timer fires; a workflow blocks on
  `wait_for_event(ReviewDecision, key=...)` and resumes when `send_event` is
  delivered; a wait `timeout` fires via the timer path.
- **Acceptance:** Durable sleep survives across the poll interval; event wait
  blocks then resumes on delivery; timeout resolves; all transitions journaled
  (`TimerCreated`/`TimerFired`/`EventWaitStarted`/`ExternalEventReceived`).

## V4 — Composite primitives + parallel crash-recovery

- **Affordances:** N5/A6.1 `satay.map` & `satay.gather` (per-item `key=`,
  `concurrency`), N7 identity for fan-out, N5/A6.2 `satay.start_child`.
- **Demo:** `satay.map` fans out over items with explicit keys; kill the worker
  mid-fan-out; on restart, **completed items are reused and only unresolved items
  re-run** (the signature demo from the planning summary §5); `gather` awaits
  mixed calls; a child workflow runs and links to its parent.
- **Acceptance:** Map items match by `key=` regardless of completion order;
  partial completion survives a crash; gather rejoins; child run linked in the
  journal/tree.

## V5 — Control & read API

- **Affordances:** N15 HTTP `start`/`status`/`cancel`/`send_event`/`fork`
  (writes to store; worker polls), N16 read API (run list, timeline, tree,
  task/attempt detail, compare), N18 redactor.
- **Demo:** Start a run via HTTP; deliver an approval via `send_event` over HTTP
  and watch a V3 workflow resume; `cancel` a run; fetch a run's timeline/tree as
  JSON with secrets redacted.
- **Acceptance:** External caller can drive a running workflow through the API;
  read endpoints return journal-derived views; redaction strips configured
  sensitive fields.

## V6 — Satay Studio web app

- **Affordances:** U2 run list, U3 timeline (incl. interruption marker),
  U4 execution tree, U5 task detail (logical vs attempts, I/O, native stack
  trace, retry reason, duration, model/token/cost), consuming N16/N18.
- **Demo:** the local server (the V5 worker + control/read API, now serving the
  bundled Studio) runs on localhost; open a run to see its timeline, drill into
  the execution tree, expand a task to see its attempts, stack trace, and recorded
  usage; the V1 interruption is visible. (The single `satay dev` command that
  boots this in one line is V8.)
- **Acceptance:** All V7-independent Studio views render from the read API; usage
  metadata shown when tasks self-reported it; sensitive fields redacted.

## V7 — Fork, run comparison, version mismatch

- **Affordances:** U6 fork control → N15 fork, U7 run comparison → N16 compare,
  U8 version-mismatch banner, N17 mismatch policy (dev warn / strict reject).
- **Demo:** Fork a completed run from before a chosen event with a changed task
  impl/prompt/input; the original is untouched and the fork re-runs downstream;
  compare the two runs side by side; resuming a run under a changed code version
  shows the mismatch banner (and is rejected under strict).
- **Acceptance:** Fork creates a new run from a journal point without rewriting
  history; comparison highlights differences; version mismatch surfaced honestly.

## V8 — `satay dev` unified stack + payload spill

- **Affordances:** U1 `satay dev`, N20 dev orchestrator (worker + SQLite +
  control API + Studio in one process), N19 blob spill (> ~256 KB → local file +
  journal reference).
- **Demo:** A single `satay dev` command boots the whole local stack; a workflow
  producing a large task output spills to a blob file while the journal keeps a
  reference, and Studio still renders it.
- **Acceptance:** One command runs everything locally; large payloads spill and
  rehydrate transparently; no regression in V1–V7 behavior.

---

## Notes

- **V1 is the critical path** — it proves the hardest property (crash recovery via
  replay) before breadth is added. Everything else layers on the same journal +
  test seam.
- **Demo-able without the web UI until V6:** V1–V5 demo through the CLI text
  timeline and the JSON API, so durability is provable early.
- **Detailed-design items** flagged in `SHAPING.md` (journal event schema &
  transaction boundaries, idempotency-key formula, ambiguous-completion detection,
  testing-utility/DI/progress-streaming API) are resolved per-slice by
  `build-plan-specs`, primarily within V1 and V2.
