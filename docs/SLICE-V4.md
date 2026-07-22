---
shaping: true
slice: V4
---

# Satay Runtime — SLICE V4: Composite primitives and parallel crash-recovery

Adds the remaining durable primitives, parallel `map`/`gather` and child workflows,
and proves the signature demo (planning summary §5): kill a worker mid-fan-out and,
on restart, only unresolved items re-run. This exercises durable-call identity by
explicit `key=` (ADR-0002) rather than by ordinal. Affordance IDs reference
`BREADBOARD.md`.

---

## Affordances

| ID | Affordance | Scope in V4 |
|----|------------|-------------|
| N5 / A6.1 | `satay.map(fn, items, key=, concurrency=)` and `satay.gather(...)` | Full: asyncio concurrency in one process (ADR-0007) |
| N7 | Identity for fan-out: explicit `key=` per item, not ordinal | Full, extending V1's ordinal resolver |
| N5 / A6.2 | `satay.start_child(workflow, ...)`: a child run linked to its parent | Full |

**Deferred or unchanged:** the control and read API (→ V5) and the Studio tree view
(→ V6). This slice reuses V2's idempotency-key derivation for keyed items and V1's
crash-recovery seam.

---

## Detailed-design items resolved in this slice

1. **Fan-out identity (ADR-0002).** Each `map` item is a durable call identified by
   `(task_name, key)`, where `key=` is a caller-supplied stable id per item. It is
   required, because item count and completion order vary and no stable ordinal
   exists. Duplicate keys within one `map` are a usage error caught at schedule time.
   `gather` items keep their own per-call identity (an ordinal, or nested `map` keys).
2. **Partial-completion recovery rule.** On resume mid-fan-out, each item is consulted
   independently against the journal by its `(task_name, key)`: completed items are
   reused, and unresolved or ambiguous items re-run (reusing V2's ambiguous rule).
   Completion order is irrelevant, since the join is by key.
3. **Child-run linkage.** `start_child` creates a new run whose `WorkflowCreated`
   records `parent_run_id` and the parent's originating durable-call identity. The
   parent journals a `ChildWorkflowScheduled` and, on the child's completion, treats
   it as a durable-call hit. This is the data the V6 execution-tree view reads.

---

## Build Plan

1. **Identity resolver, the `key=` path (N7).** Extend the V1 resolver so `map` and
   `gather` items resolve identity by explicit `key=` instead of by ordinal. Validate
   presence and uniqueness of keys at schedule time with a clear error.

2. **`satay.map(fn, items, key=, concurrency=N)` (A6.1).** For each item, form a
   keyed durable call and schedule it. Run up to `N` concurrently on the asyncio loop
   (a bounded semaphore). Each item independently consults the journal (hit reuses,
   miss executes via the `LocalTaskExecutor`). Rejoin results in input order
   regardless of completion order.

3. **`satay.gather(*durable_calls)` (A6.1).** Await several heterogeneous durable
   calls together (tasks, other maps, child workflows), each keeping its own identity,
   and rejoin results positionally. Concurrency is bounded by the same loop.

4. **Partial-completion recovery.** Make the crash and restart path reuse completed
   keyed items and re-run only unresolved ones (design rule 2). Add a fault-injection
   point mid-fan-out, after some but not all items have recorded `TaskCompleted`.

5. **`satay.start_child(workflow, input, ...)` (A6.2).** Create a linked child run
   (design rule 3): journal `ChildWorkflowScheduled` on the parent, create the child's
   `WorkflowCreated` with parent linkage, drive it, and surface its result to the
   parent as a durable-call result reused on parent replay. The child is a full run
   with its own journal, inspectable on its own and in the parent's tree.

6. **Execution-tree data shape.** Make parent and child links and map-item grouping
   recoverable from the journal (parent ref, originating call identity, item keys), so
   V6 can render the tree with no extra bookkeeping.

7. **Demo and tests.** A `map` over items with explicit keys, crashed mid-fan-out and
   restarted so only unresolved items re-run (proven by per-item execution markers); a
   `gather` over mixed calls; and a child workflow linked to its parent.

---

## Demo

`satay.map` fans out over items with explicit keys; kill the worker mid-fan-out; on
restart, completed items are reused and only unresolved items re-run (the signature
demo). `gather` awaits mixed calls and rejoins. A child workflow runs and links to
its parent.

---

## Test Plan

Everything runs through the V1 seam, with the fault-injection hook placed mid-fan-out
to prove partial recovery. Per-item execution markers show which items re-ran. Per
ADR-0011 (H3) the integration tier is boundary-only: the near-verbatim twins of the
E2E cases (partial recovery, positional gather, child linkage, concurrency bound) are
dropped, and key validation lives once in the unit tier. The composite failure paths
below follow the fail-fast semantics of ADR-0020.

### End-to-End Tests

- `map` items match by `key=` regardless of completion order.
- Partial completion survives a crash: completed items are reused and only unresolved
  items re-run (verified by per-item execution markers) — the signature demo.
- `gather` rejoins heterogeneous durable calls, preserving positional results.
- A child run is linked to its parent in the journal and tree, and its result is
  reused on parent replay.
- `concurrency=N` bounds in-flight items to N within the single process, and an
  unspecified `concurrency=` uses the default bound.
- A failed `map` item raises through the `map` (fail-fast, ADR-0020); in-flight
  siblings settle but their results are discarded.
- A failed `gather` member fails the whole `gather` (fail-fast, ADR-0020).
- A failed child workflow's `WorkflowFailed` surfaces to the parent as a raised
  exception, and re-raises deterministically from the journal on parent replay
  (ADR-0020).
- A child crashed mid-flight is re-awaited on parent resume and the child resumes
  (not restarted from scratch); already-completed children are reused.

### Integration Tests

- A `map` rejoins results in input order even when items complete out of order.
- A nested `map` (a `map` inside a `gather`) resolves each item's identity by its
  nested `key=`, independent of the ordinal counter.

### Unit Tests

- A missing item `key` raises at schedule time.
- Duplicate keys within one `map` are rejected at schedule time.
- The `key=` identity resolves independently of the ordinal counter.
- Idempotency-key derivation is distinct across `map` keys (the map-key case
  relocated from V2, where map keys do not yet exist).
- Tree linkage is derivable from a parent ref plus item keys.

---

## Dependencies

- **Upstream:** V1 (replay, journal, executor, crash seam) and V2 (idempotency-key
  derivation, ambiguous-completion rule for re-run).
- **Downstream:** V6 renders the execution tree (child workflows, map items); V5's
  read API exposes the tree view built from this linkage.
