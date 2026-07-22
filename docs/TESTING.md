---
shaping: true
---

# Satay Runtime — Test Plan Audit (step H2)

> Audit of the `## Test Plan` section of every `SLICE-V*.md`, produced 2026-07-21
> as step **H2** of `/build-plan-specs`. Read against FRAME.md, CONTEXT.md,
> ARCHITECTURE.md and the ADRs, with **ADR-0011** as the test strategy of record:
> the primary seam is the public API driving real workflows against a temp SQLite
> store, with the fault-injection hook and manual clock, asserting on **observable
> outcomes** (result, status, journal) — *never on replay internals*.
>
> Findings answer the five H2 questions: missing tests, duplicate/overlapping
> tests, miscategorised tests, tests not specific to their slice, and tests that
> reveal a planning or architecture insight. Decision-requiring items are raised as
> **Q40–Q51** in QUESTIONS.md and cross-referenced below. Nothing here edits the
> slice files; that is H4.

---

## Executive summary

The eight test plans are individually reasonable and all conform to the ADR-0011
seam, but the audit surfaces four structural issues that matter more than any single
missing test:

1. **The integration tier is largely a mirror of the E2E tier.** In V2, V3, V4, V7
   and V8, nearly every integration test restates an E2E test one level down. Because
   ADR-0011 makes the *public API* the primary seam, the E2E tier already exercises
   those code paths; the integration tier earns its keep only when it isolates a
   component boundary the E2E cannot (store `seq` allocation, codec, backoff math,
   inbox matching). See **cross-cutting finding A** and **Q40**.

2. **Two headline properties are untested anywhere.** The local-surface security
   guard (ADR-0014: session token + `Origin`/`Host` allow-list) and the
   non-blocking-reads guarantee (ADR-0012: "the debugger never blocks on the worker")
   are both load-bearing decisions with no test in any slice. See **E** and **F**;
   **Q43**, **Q51**.

3. **Determinism has a hole the manual clock does not cover.** Backoff *jitter*
   (V2) is randomness in the runtime; the manual clock controls time but not the RNG,
   so "deterministic under the test clock" is not achievable without an injected seed
   seam. See **K**; **Q46**.

4. **Several ownership boundaries are ambiguous**, so the same behavior is tested in
   two or three slices: the interruption "gap" marker (V1 CLI / V6), the compare
   endpoint (V5 / V7), the "fixed" read-API contract (extended by V2/V4/V7), and
   redaction (V5 / V6, bypassed by V8 spill). See **C, D, H, I**; **Q42, Q44, Q45**.

---

## Cross-cutting findings

### A. The integration tier mirrors the E2E tier (structural) — Q40
Across V2/V3/V4/V7/V8 the integration list is a near-1:1 restatement of the E2E list.
Examples (E2E → its integration twin):

- V4 "partial completion survives crash, only unresolved re-run" → "a map crashed
  mid-fan-out reuses completed keyed items and re-runs only unresolved ones."
- V4 "gather rejoins heterogeneous calls, positional" → "a gather over a task, a map,
  and a child returns positional results."
- V3 "timer firing is idempotent, no double-resume" → "a duplicate timer fire does not
  double-resume."
- V8 "`satay dev` runs worker/SQLite/API/Studio, prints URL" → "`satay dev` starts
  worker/store/API/Studio and serves the URL."

Under ADR-0011 the E2E tier drives the same public API and asserts the same observable
outcome, so the twin adds cost without new coverage. **Recommendation:** recast each
slice's integration tier to *only* the boundary tests that add isolation or diagnostic
value (store, codec, resolver, backoff, inbox, poll loop, redactor) and delete pure
restatements. Confirm the philosophy in **Q40** before applying in H4.

### B. Some integration tests assert on internals, against ADR-0011 — Q41
ADR-0011 says assert on observable outcomes, "never on replay internals," yet:

- V1 "the replay engine returns a recorded result on a journal hit **without invoking
  the executor**" — asserts the executor was *not* called (an internal spy).
- V1/V2 "…then appends `TaskCompleted`" style assertions inspect engine sequencing.

"Reused vs re-executed" is meant to be proven by the **execution-count marker**
(observable), which every slice already has. **Recommendation:** reframe these to
assert via the marker and the journal, not via executor call-spying — or explicitly
sanction internal spying as a deliberate exception. See **Q41**.

### C. The interruption "gap" marker is undefined and detected in three places — Q42
The ⚡ interruption/resume marker is rendered by the V1 CLI (build step 12,
"wherever a `WorkflowResumed` follows a gap"), and independently by V6 (integration
"marks the interruption where a `WorkflowResumed` follows a gap"; unit
"interruption-marker detection fires on the right event sequence"). What a "gap" *is*
is never defined, and detection logic is implemented twice. **Recommendation:** define
the rule once (candidate: mark on every `WorkflowWaiting`→`WorkflowResumed` transition,
which is precise and needs no fuzzy "gap"), own it in one layer, test it once. See
**Q42**.

### D. The "fixed" V5 JSON contract is not actually fixed — Q45
V5 calls its read-API contract "fixed" and "load-bearing," but it is extended by later
slices: the V2 usage slot feeds task detail, V4 tree linkage feeds the tree endpoint,
and V7 adds a version-mismatch field and `RunForked` lineage. V6 tests written against
the "fixed" contract will break when V7 extends it. **Recommendation:** declare the
contract *extensible and additive*, enumerate the fields V2/V4/V7 add, and make V6 view
tests tolerant of unknown/added fields. See **Q45**.

### E. Local-surface security (ADR-0014) is untested in every slice — Q43
Session token, `Origin`/`Host` allow-list, loopback bind, random port — none are
tested anywhere. V5 is where the HTTP surface is born and V8 (`satay dev`) is where the
per-session token is issued. **Recommendation:** add negative tests (no/invalid token →
rejected; disallowed `Origin`/`Host` → rejected; non-loopback bind refused) and assign
them to a slice. Ownership depends on where the token is issued — see **Q43**.

### F. The non-blocking-reads guarantee (ADR-0012) is untested — Q51
"The debugger never blocks on the worker" is the reason for the two-thread /
WAL-read-only design, and nothing asserts it. **Recommendation:** a V5 test that uses
the fault-injection hook to stall/pause the worker mid-write and asserts a read
endpoint still returns promptly. See **Q51**.

### G. Time-based assertions must name the manual clock (hygiene)
V3 and V5 assert timing ("resolves via the timer path"; "takes effect within one poll
interval"). These are only deterministic if driven through the V1 manual-clock seam.
**Recommendation:** every such test should state it advances virtual time via the
manual clock; none should wait on wall-clock. Low effort, prevents flakiness.

### H. Cross-slice tests sitting in the wrong slice — Q44
- **V2 unit** "idempotency-key derivation distinct across ordinals **and map keys**"
  references *map keys*, a V4 concept. Acceptable only as a pure-function test of the
  formula; otherwise move the map-key case to V4.
- **V2 E2E** "`NondeterminismError`… dev warns **and offers to fork**" — fork is V7;
  in V2 only the *warn* is realisable. Trim the "offers fork" clause to V7.
- **Compare endpoint** is claimed by both V5 (build step 5 implements it; unit
  "compare aligns two runs by identity") and V7 (affordance "N16 compare: Full";
  E2E/integration on compare). Endpoint vs view split is blurred. Resolve in **Q44**.

### I. Redaction is tested at V5/V6 but bypassed by V8 spill (coverage hole) — Q49
Redaction (N18) is exercised on inline journal data in V5 and V6. V8 moves large
payloads to blob files behind `output_ref`. No test confirms a **secret in a spilled
payload** is still redacted on read. **Recommendation:** add a V8 test: large output
containing a configured sensitive field spills to a blob and is still redacted through
the read API. See **Q49**.

### J. `satay runs show` is only tested for the V1 event subset — Q50
The CLI renders the V1 events and is tested in V1. V3 (timers/events), V4 (map/child),
and V7 (`RunForked`) all add event types the CLI would presumably render, but no slice
re-tests CLI rendering of the new types. **Recommendation:** decide whether the CLI is
frozen at the V1 subset (state it) or grows per slice (add one rendering test per new
event family). See **Q50**.

### K. Backoff jitter is not deterministic under the manual clock alone — Q46
V2 asserts backoff is "exponential with jitter… and deterministic under the test
clock." The manual clock controls *time*, not the *RNG* that produces jitter. Without
an injected/seeded RNG seam (a sibling of the clock seam), the schedule is not
reproducible and the "within bounds" unit test can only assert bounds, not exact
values. **Recommendation:** introduce an injected RNG/seed seam for jitter. See
**Q46** (may warrant a small ADR).

---

## Per-slice findings

### V1 — Durable two-task core with text timeline

**Missing**
- **Crash *before* the first `TaskCompleted`.** The headline test injects a crash
  *after* `TaskCompleted` for `step_one` (proving *reuse*). The complementary half —
  a crash after `TaskScheduled`/`TaskAttemptStarted` but before `TaskCompleted`, where
  the task **re-runs** on resume — is not tested, though it is core V1 replay behavior
  (a journal miss). Add it. (This is the un-formalised precursor of V2's ambiguous
  rule; V1 owns the plain miss-→-re-run case.)
- **The V1 lightweight determinism guard** (build step 9: colliding `(task_name,
  ordinal)` with a different task name → plain error) has **no test**. It is built in
  V1 and only replaced in V2, so it should be covered while it exists.
- **`WorkflowResumed` is appended on resume** — asserted only implicitly via the CLI
  marker. Add a direct journal assertion (feeds C/Q42).
- **`start` against an already-terminal `run_id`** — resume path is defined only for
  non-terminal runs; behavior for a completed run (expected: no-op, return result) is
  unspecified and untested.
- **Unannotated rehydration → dict** is asserted at E2E but the unit tier only covers
  Pydantic and dataclass reconstruction, not the annotation-absent → decoded-dict
  fallback. Add the fallback unit case.

**Duplicate / overlapping** — minimal within V1; the tiers are well separated.

**Miscategorised**
- Integration "the replay engine returns a recorded result on a journal hit without
  invoking the executor" spies on an internal (finding **B**/Q41); reframe via the
  execution-count marker.

**Not slice-specific** — none.

**Planning / architecture insight**
- Integration "`SQLiteStore.append` allocates a monotonic per-run `seq` **under
  concurrent appends**" describes concurrency the single-writer model (ADR-0007/0012)
  forbids: one worker thread is the sole writer, so there are never concurrent appends
  to one run. Either the test is validating a lock the architecture makes unnecessary
  (over-testing), or the wording contradicts the writer model. Reword to "monotonic
  `seq` across sequential appends, and correct per-run isolation across interleaved
  runs." (Minor; folded into H4, no separate question.)

### V2 — Guarantees: retries, at-least-once, idempotency, nondeterminism

**Missing**
- **`@task(timeout=)` enforcement** (build step 2 enforces it) is untested at every
  tier — no test that an over-running task fails its attempt and retries. Genuine gap.
- **Retry exhaustion → `WorkflowFailed`.** Fail-twice-then-succeed is covered; the
  all-attempts-fail terminal path is not.
- **The idempotency key's *purpose*** — external-effect de-duplication — is untested.
  Tests prove the key is *stable* and *distinct*, but none prove a key-guarded side
  effect actually runs once across an at-least-once re-run. The demo describes exactly
  this ("proven by the idempotency-key-guarded marker"); add the test.
- **`effect_safety=warn` logs / `off` is silent** — E2E "warn/off behave accordingly"
  is too vague to be a test (see planning insight below).

**Duplicate / overlapping** (finding **A**)
- E2E and integration restate each other for: ambiguous-completion re-run; keyed
  `start`; `NondeterminismError`; `effect_safety=strict` rejection; `record_model_usage`
  persistence. Keep the E2E acceptance test; keep the integration test *only* where it
  isolates a boundary (e.g. "keyed start resolves via the store **index**" is a genuine
  store-boundary test; "effect_safety=strict rejects…" is a straight duplicate — drop
  one).

**Miscategorised** — see **B**/Q41 for internal-spying phrasing.

**Not slice-specific** (finding **H**/Q44)
- Unit "distinct across… **map keys**" (map is V4) and E2E "offers to fork" (fork is
  V7). Trim/relocate.

**Planning / architecture insight**
- **Jitter determinism** needs an RNG seam (finding **K**/Q46).
- "warn/off behave accordingly" is unfalsifiable as written; the plan lacks an
  observable for the `warn`/`off` modes. This implies a logging/observation seam so the
  warn path is assertable (capture the `satay` logger). Sharpen to "warn emits a
  `satay`-logger warning; off emits nothing."

### V3 — Timers and events

**Missing**
- **Timeout-vs-event race** in `wait_for_event(timeout=)`: when a matching event and
  the timeout are both pending, which resolves, and is it deterministic? Undefined and
  untested. Design gap → **Q48**.
- **Multiple buffered events matching one `(type, key)`** — consumption order (FIFO?)
  is unspecified and untested. → **Q48**.
- **Unmatched inbox event at run end** (design rule 3: "persists until matched or the
  run ends") — no test of the run-ends disposition of an unconsumed event.

**Duplicate / overlapping** (finding **A**) — E2E/integration mirror on: sleep
release+resume; event wait release+resume; event-before-wait buffering; idempotent
timer fire. Narrow integration to the poll-loop and inbox-query boundaries.

**Miscategorised** — none material; the three unit tests (due-check, inbox match,
`fire_at` arithmetic) are correctly unit-level and genuinely useful.

**Not slice-specific** — none.

**Planning / architecture insight**
- Timing assertions must ride the manual clock (finding **G**).

### V4 — Composite primitives and parallel crash-recovery

**Missing**
- **map / gather partial-failure semantics** — if one item fails, does the whole
  composite fail (asyncio.gather default), or are results/exceptions collected? Neither
  designed nor tested. Central semantic gap → **Q47**.
- **Child-workflow failure propagation** — how a failed child surfaces to the parent's
  durable call (raises? recorded as failed hit?) is undefined and untested → **Q47**.
- **Child crash mid-flight** — parent replay reusing a *completed* child is tested;
  a crash while the child is in flight (parent must re-await, child resumes) is not.
- **`concurrency=` default** when unspecified — untested.
- **Nested map (map inside gather)** — the design mentions nested `map` keys but no
  test covers identity resolution for a nested fan-out.

**Duplicate / overlapping** (finding **A**, worst here) — every E2E has a near-verbatim
integration twin (partial recovery, positional gather, child linkage, concurrency
bound), and key validation is tested three times (E2E + two unit). Keep the two unit
key-validation tests (missing key, duplicate key) as the real home; drop the E2E
restatement of validation and the integration twins that add no isolation.

**Miscategorised** — none material.

**Not slice-specific** — none (V4 is where `key=` legitimately lands).

**Planning / architecture insight**
- The absence of a designed failure semantics for `map`/`gather`/child (Q47) is the
  real signal: the *happy-path* fan-out is well specified but the *failure* algebra of
  composites is a hole that will force redesign if discovered during implementation.

### V5 — Control and read API

**Missing**
- **Security guard (ADR-0014)** — no token/`Origin`/`Host`/loopback tests (finding
  **E**/Q43). Highest-priority gap in the slice.
- **Non-blocking reads under a stalled worker (ADR-0012)** — untested (finding
  **F**/Q51).
- **Cancel mid-task settles cleanly** (build step 3) — only "halts within one interval"
  is tested; the in-flight-task-at-cancel behavior is not.
- **Read-API error paths** — 404 for an unknown run, malformed body on writes — untested.

**Duplicate / overlapping** — start/cancel/send-event appear as one bundled E2E and
three split integration POSTs; redaction spans E2E+integration+unit; fork-route
validation spans E2E+integration+unit. Keep one acceptance test per behavior and the
unit tests; drop the middle integration restatements that don't isolate a boundary.

**Miscategorised** — none material.

**Not slice-specific** (finding **H**/Q44)
- The **compare endpoint** is built and unit-tested here but also fully claimed by V7.
  Decide the endpoint(V5)/view(V7) split and place tests accordingly.

**Planning / architecture insight**
- Calling the JSON contract "fixed" while V6/V7 extend it (finding **D**/Q45) is the
  planning smell; treat it as additive from the start.

### V6 — Satay Studio web app

**Missing**
- No smoke test that the SPA bundle **builds and loads** — likely a deliberate
  non-goal under ADR-0011 (verify via JSON, not UI), but state it explicitly so the
  gap is intentional, not accidental.
- Run-list ordering ("most-recent-first", build step 2) is untested.

**Duplicate / overlapping**
- "Every V6 view renders from the V5 read API" and "sensitive fields redacted before
  the browser" largely **re-test V5** (read endpoints + redaction). The genuinely new
  V6 content is the *view-model transform* (interruption marker, tree grouping, attempt
  grouping) — thin, and partly duplicated with V1 (marker) per finding **C**.

**Miscategorised (the slice-level smell)**
- Because ADR-0011 verifies Studio *through the JSON API, not through UI rendering*,
  V6's "E2E" tests are not end-to-end (no browser); they are view-model/API assertions
  at the same altitude as its "integration" tests. The three-tier split is **forced**
  here. **Recommendation:** collapse V6 into one tier ("view-model tests over the read
  API") or relabel "E2E" as "acceptance (via read API)"; keep only the transforms that
  are V6-specific (marker, grouping, no-usage rendering).

**Not slice-specific**
- Interruption-marker detection (finding **C**/Q42) is shared with V1; own it once.

**Planning / architecture insight**
- V6 having little to test *of its own* is the expected consequence of ADR-0011 — a
  healthy signal that runtime behavior was proven earlier. The fix is to stop
  re-asserting V5, not to invent UI tests.

### V7 — Fork, run comparison, version mismatch

**Missing**
- **Fork of a non-terminal (running/waiting) run** — only a completed run is
  demonstrated; forking a live run's semantics are undefined/untested.
- **Fork of a fork** (lineage chains) — untested.
- **Compare of two *unrelated* runs** — design says "any two runs can be compared" but
  only run-vs-fork is tested.
- **Mismatch banner data source** — the banner is a Studio element, so mismatch state
  must arrive via a read-API field; V5's contract has no such field (finding **D**/Q45).
  Add the field + its read-API test, or the banner has nothing to render.

**Duplicate / overlapping** (finding **A**) — "source journal unchanged after fork"
(E2E + integration), `RunForked` lineage (E2E + unit verbatim), version-mismatch policy
(E2E + integration), compare differences (E2E + integration, plus a V5 unit). Collapse
to one acceptance + boundary units.

**Miscategorised** — none material.

**Not slice-specific** — the compare overlap with V5 (finding **H**/Q44).

**Planning / architecture insight**
- The mismatch banner (Q45) and fork-of-running (above) show the read-API contract and
  the fork semantics were scoped for the *completed-run* case; the *live-run* cases need
  an explicit decision before V7 implementation.

### V8 — `satay dev` unified stack and payload spill

**Missing**
- **Redaction of a spilled payload** (finding **I**/Q49) — a secret in an
  over-threshold output must still be redacted on read.
- **Blob lifecycle** — orphaned blobs on run deletion, and whether a **fork** copies or
  shares a referenced blob (source must stay byte-for-byte unchanged, per V7). Undefined.
- **Two `satay dev` on one `./.satay/`** — WAL single-writer would be violated; ADR-0017
  implies refusing, but there's no test of the lock/refusal. (May be out of MVP scope —
  state it.)

**Duplicate / overlapping** (finding **A**) — `satay dev` boot (E2E ≈ integration
verbatim), spill decision (E2E + integration + unit), rehydration (E2E + integration),
and regression (E2E "no regression V1–V7" superset of integration "V1 crash-recovery
passes unchanged"). Keep the E2E boot + full regression, the unit boundary tests, and
one integration rehydration test; drop the verbatim twins.

**Miscategorised** — none material.

**Planning / architecture insight**
- **The spill threshold must be exact, not "~256 KB / roughly."** A boundary unit test
  ("fires at the roughly 256 KB threshold boundary") is not writable against a fuzzy
  threshold. Pin an exact byte count (finding in **Q49**). This is the kind of
  planning imprecision a boundary test is designed to flush out — good that it surfaced
  here rather than in code.

---

## Open questions raised by this audit

Recorded in QUESTIONS.md as **Q40–Q51** (continuing from the resolved Q39). **All were
resolved in H3 (2026-07-22); Jian accepted every lean.** The resolution for each is in
the ADR named below; H4 will edit the slice `## Test Plan` sections to match. Index:

| Q | Findings | Resolution (H3) | Where |
|---|----------|-----------------|-------|
| Q40 | A | Integration tier = boundary-only; E2E mirrors dropped | ADR-0011 |
| Q41 | B | Assert via execution-count marker + journal, never internal spying | ADR-0011 |
| Q42 | C | Marker = `WorkflowWaiting`→`WorkflowResumed`, owned once in the view layer | ADR-0009 |
| Q43 | E | Guard enforced + negative-tested from V5; `satay dev` issues token (V8 smoke test) | ADR-0014 |
| Q44 | H, D | Compare endpoint owned/tested in V5; V7 owns only the view | ADR-0009 |
| Q45 | D | Read-API contract additive/forward-compatible; V6 tests tolerate added fields | ADR-0018 |
| Q46 | K | Seedable RNG seam alongside the manual clock for deterministic jitter | ADR-0011 |
| Q47 | V4 | Fail-fast for `map`/`gather`/`start_child`; collect-style deferred | ADR-0020 (new) |
| Q48 | V3 | Event wins over simultaneous timeout; FIFO by `received_at` | ADR-0021 (new) |
| Q49 | I, V8 | Threshold pinned at 262144 B; redact after rehydration; spilled-secret test | ADR-0004, ADR-0014 |
| Q50 | J | `satay runs show` frozen at the V1 event subset; Studio covers the rest | ADR-0016 |
| Q51 | F | Add a stalled-worker read test via the fault-injection hook | ADR-0011, ADR-0012 |

All findings above therefore have an owning decision. H4 applies them to the slice
`## Test Plan` sections: prune the E2E/integration mirrors (Q40), add the failure-path,
security, non-blocking-read, spill-redaction, and event-ordering tests (Q43/Q47/Q48/Q49/Q51),
and relocate the mis-sliced cases (Q44, and the V2 map-key/offers-fork trims).
