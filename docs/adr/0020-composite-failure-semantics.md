# ADR-0020 — Failure semantics of map, gather, and child workflows

- **Status:** Accepted
- **Date:** 2026-07-22
- **Deciders:** Jian (leejianrong2@gmail.com)

## Context

The composite primitives introduced in V4 (`satay.map`, `satay.gather`,
`satay.start_child`) each fan work out into several durable calls, but the slice
specified only the happy path. The H2 test audit (TESTING.md, Q47) found no decision
for what happens when one part fails: does a failed `map` item abort the whole map or
leave the others running; does a failed `gather` member fail the group; and how does a
failed child workflow surface to the parent's durable call. Without a rule, the failure
behaviour would be decided implicitly during implementation and be untestable.

The three primitives:

- **`map(fn, items, key=, concurrency=)`** runs one task over many items in parallel;
  each item is its own keyed durable call.
- **`gather(*calls)`** awaits several heterogeneous durable calls together (a task,
  another map, a child), each keeping its own identity.
- **`start_child(workflow, ...)`** launches a linked child run with its own journal.

## Decision

**Fail-fast, matching native `await` semantics.** When an item, a `gather` member, or a
child workflow raises, the exception propagates through the composite: the `map` /
`gather` / `start_child` call raises, exactly as an ordinary awaited call would in
Python. A failed child's terminal `WorkflowFailed` is surfaced to the parent's durable
call as a raised exception, and is re-raised deterministically from the recorded
journal on parent replay (once-recorded logical completion, ADR-0006). In-flight
siblings are allowed to settle but their results are discarded once the composite has
raised.

A **collect-style** mode (gather everything, return results alongside exceptions, in the
manner of `asyncio.gather(return_exceptions=True)`) is **deferred post-MVP**. It would
be added as an explicit opt-in, never the default.

## Consequences

- Keeps the product promise of ordinary async Python with native exceptions (FRAME,
  ADR-0005); no framework-specific error-aggregation type in the MVP surface.
- Crash recovery is unchanged: completed keyed items are reused on resume, and a failure
  re-raises from the journal rather than re-running a completed logical call.
- A workflow that needs partial-results tolerance must wait for the deferred collect
  mode, or structure its own per-item error handling inside tasks.
- V4's test plan gains failure-path cases (item failure, gather-member failure, child
  failure and its replay), tracked in TESTING.md.
- Builds on ADR-0006 (execution guarantees) and ADR-0002 (durable-call identity).
