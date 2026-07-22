---
shaping: true
slice: V7
---

# Satay Runtime — SLICE V7: Fork, run comparison, version mismatch

This slice delivers the payoff of an append-only journal: because history is never
rewritten, you can branch a run from an earlier point and re-run it under changed
code, and you can put the two runs side by side to see what the change did. It also
turns V1's code-version stamp into a real policy, so resuming under changed code is
caught instead of silently diverging. Affordance IDs reference `BREADBOARD.md`;
the decisions of record are ADR-0004 (fork) and ADR-0010 (versioning).

---

## Affordances

| ID | Affordance | Scope in V7 |
|----|------------|-------------|
| U6 | Fork control in Studio: "fork from before event N", wiring to the N15 fork endpoint | Full |
| N15 fork | Fork semantics: a new run branched from a journal point, original untouched, downstream re-runs | Full (the route stub from V5 gets its real behavior) |
| U7 | Run comparison: two runs side by side, wiring to the N16 compare endpoint | Full |
| N16 compare | Comparison view over two runs | Full |
| U8 | Version-mismatch banner on affected runs | Full |
| N17 policy | Mismatch policy: dev warns and offers fork, strict rejects | Full (upgrades V1's stamp-only) |

---

## Detailed-design items resolved in this slice

1. **What a fork is, concretely.** A fork creates a new run whose journal begins as
   a copy of the source run's events up to the chosen fork point, then diverges. A
   `RunForked` event records the source run and the fork-point event so lineage is
   traceable. The source run is never touched, which is the whole reason the
   journal is append-only (ADR-0004). Downstream durable calls after the fork point
   are misses in the new run, so they re-run, picking up any changed task
   implementation, prompt, input, or retry policy. **The MVP forks only terminal
   runs** (`completed`/`failed`/`cancelled`); a fork of an actively-executing run is
   rejected with a clear error naming the run's status, because a growing journal head
   adds a fork-point race and concurrent-divergence semantics with no MVP payoff
   (ADR-0004/Q53). The guard is a status allow-list, so widening it to quiescent
   `waiting` runs later is a one-line change. Any referenced payload that has spilled
   to a blob is **shared** by the fork, not copied, keeping the source byte-for-byte
   unchanged (blobs are immutable; ADR-0004/Q54).
2. **What comparison shows.** Two runs are aligned by durable-call identity, and
   the compare view marks where their inputs, outputs, attempts, or timing differ.
   The common case is a run and its fork, which answers "what did my change do",
   but any two runs can be compared.
3. **Mismatch policy, reusing an existing split.** On resume, the worker compares
   the run's stamped code version (V1) against the current one. If they differ,
   dev mode warns and offers to fork, strict mode rejects the resume. This is the
   same dev-warn / strict-reject split already used for nondeterminism (V2) and
   effect safety, so there is one mental model, not three.

---

## Build Plan

1. **Fork semantics behind the V5 route (N15 fork).** Give the fork endpoint its
   real behavior: given a source run and a fork-point event, create a new run,
   seed its journal from the source up to that point, append `RunForked` with the
   lineage, and hand it to the worker to drive. Downstream calls re-run because
   they are journal misses in the new run.

2. **Fork control in Studio (U6).** Add a "fork from before this event" control on
   the timeline that calls the fork endpoint and navigates to the new run. The
   source run's view is unchanged after forking, which is the property to
   demonstrate.

3. **Compare endpoint and view (N16 compare, U7).** Implement `compare` to align
   two runs by durable-call identity and surface differences in inputs, outputs,
   attempts, and duration. Build the Studio side-by-side view on top of it.

4. **Version-mismatch policy (N17).** On resume, compare stamped versus current
   code version. On a mismatch, apply the policy: dev warns and offers a fork,
   strict rejects the resume. No automatic migration (ADR-0010).

5. **Version-mismatch banner (U8).** Surface the mismatch on affected runs in
   Studio, linking the warning to the fork control so the offered path is one
   click away.

6. **Demo and tests.** Fork a completed run from before a chosen event with a
   changed task, confirm the original is untouched and the fork re-runs downstream,
   compare the two, and resume a run under a changed version to see the banner in
   dev and the rejection in strict.

---

## Demo

Fork a completed run from before a chosen event with a changed task
implementation, prompt, or input. The original is untouched and the fork re-runs
downstream. Compare the two runs side by side. Resuming a run under a changed code
version shows the mismatch banner, and is rejected under strict.

---

## Test Plan

Fork and mismatch behavior run through the V1 seam and the V5 routes. The key
property to assert is that the source run's journal is byte-for-byte unchanged after
a fork. Per ADR-0011 (H3) the integration tier is boundary-only: the E2E twins
(source-unchanged, `RunForked` lineage, mismatch policy) collapse to one acceptance
test plus the boundary units. The **compare endpoint** is owned and tested in V5
(ADR-0009, H3); V7 asserts only the side-by-side **view** rendered from it. The MVP
forks only **terminal** runs (ADR-0004/Q53); a fork of an actively-executing run is
covered here only by a negative (rejection) test, with quiescent `waiting`-run fork
deferred post-MVP.

### End-to-End Tests

- A fork creates a new run from a journal point without rewriting the source run's
  history, and its downstream re-runs under the change.
- `RunForked` records the source run and fork point, so lineage is traceable, and a
  fork of a fork produces a correct lineage chain.
- Forking an actively-executing run is rejected with a clear error naming the run's
  status (MVP forks terminal runs only; live-run fork deferred, ADR-0004/Q53).
- Comparison aligns two runs by durable-call identity and highlights their
  differences, both for a run-vs-its-fork and for two unrelated runs.
- A version mismatch on resume warns in dev (offering a fork) and is rejected in
  strict, following the same policy split as nondeterminism and effect safety.
- The mismatch banner appears on affected runs in Studio, reading the
  version-mismatch field the read API now exposes (additive contract, ADR-0018 H3).

### Integration Tests

- Fork seeds a new run's journal to the fork point and re-runs downstream under a
  changed task.
- The source run's journal is byte-for-byte unchanged after a fork.
- The read API exposes a version-mismatch field on affected runs — the mismatch
  banner's data source.
- The compare view renders the aligned differences returned by the V5 compare
  endpoint (the endpoint itself is tested in V5).

### Unit Tests

- `RunForked` records the source run and the fork-point event.
- Version comparison detects a mismatch between stamped and current versions.
- Fork-point selection is validated against the source journal.

---

## Dependencies

- **Upstream:** V1 (append-only journal and code-version stamp), V5 (fork and
  compare routes, read API), V6 (Studio shell the new views plug into).
- **Downstream:** V8 runs all of this under the unified `satay dev` command with
  no behavioral change.
