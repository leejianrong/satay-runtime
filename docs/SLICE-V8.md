---
shaping: true
slice: V8
---

# Satay Runtime — SLICE V8: `satay dev` unified stack and payload spill

The final slice ties the pieces into one command and removes the last size limit
on durable data. Everything from V1 through V7 has run as a server process you
start by hand; `satay dev` boots the whole local stack in one line. Separately,
payloads larger than the journal's inline threshold spill to a local blob file and
the journal keeps only a reference, so a task can return a large document without
bloating the event log. Affordance IDs reference `BREADBOARD.md`; the decisions of
record are ADR-0007/0009 (the dev process) and ADR-0004 (spill).

---

## Affordances

| ID | Affordance | Scope in V8 |
|----|------------|-------------|
| U1 | `satay dev` command | Full |
| N20 | Dev process orchestrator: worker, SQLite, control API, and Studio in one process | Full |
| N19 | Blob spill: payloads over about 256 KB go to a local file with a journal reference | Full |

Neither affordance changes workflow semantics. `satay dev` is a launcher over the
V5 server and V6 Studio, and spill is a storage detail hidden behind the
`input_ref`/`output_ref` indirection V1 already put in place.

---

## Detailed-design items resolved in this slice

1. **What `satay dev` starts.** One process containing the asyncio worker, the
   SQLite store, the HTTP control and read API, and the served Studio frontend
   (ADR-0009). It takes the database path and port as configuration and is the
   blessed local entry point. The reason this is a slice of its own, rather than
   folded into V5, is that a single-command orchestrator is only worth building
   once the parts it orchestrates exist and are proven.
2. **How spill stays invisible.** V1 stored every payload behind an
   `input_ref`/`output_ref` indirection precisely so this slice could change what
   the reference points at without a schema change. Below the threshold a
   reference resolves to an inlined value; above it, the value is written to a
   local blob file and the reference points at the file. Rehydration on read and
   on replay resolves the reference either way, so nothing upstream of the store
   knows spill happened.

---

## Build Plan

1. **Dev orchestrator (N20).** Build the component that starts the worker loop,
   opens the SQLite store, starts the HTTP control and read API, and serves Studio,
   all in one process, with clean startup and shutdown. This is assembly of
   existing parts, not new runtime behavior. As part of startup it acquires an
   **exclusive OS advisory lock on a lockfile in the data directory**, so a second
   `satay dev` on the same `./.satay/` is refused with a clear error naming the
   holding process rather than silently racing the single-writer journal into
   corruption; the lock is released on clean shutdown (ADR-0017/Q54).

2. **`satay dev` command (U1).** Add the CLI command that runs the orchestrator,
   taking database path and port options and printing the local Studio URL on
   start. This sits next to the V1 `satay runs show` command in the same CLI.

3. **Blob spill on write (N19).** In the store's payload path, when an encoded
   value exceeds the threshold (about 256 KB, tunable per ADR-0004), write it to a
   local blob file and store a reference in the journal instead of the inline
   value. Below the threshold, inline as before.

4. **Rehydration on read and replay.** Make reference resolution transparent
   wherever payloads are read: replay reuse, the read API, and Studio. A spilled
   payload rehydrates to the same value an inlined one would, so replay,
   comparison, and rendering are unaffected.

5. **Regression pass.** Run the V1 through V7 demos and their tests under
   `satay dev` to confirm the unified command changes nothing about behavior, and
   confirm a large payload spills and rehydrates without any workflow noticing.

6. **Demo and tests.** Boot the full stack with one `satay dev`, run a workflow
   whose task returns a large output, confirm the output spilled to a blob file
   while the journal holds a reference, and confirm Studio still renders it.

---

## Demo

A single `satay dev` command boots the whole local stack. A workflow producing a
large task output spills it to a blob file while the journal keeps a reference, and
Studio still renders it.

---

## Test Plan

The regression pass is the point of this slice's tests: the V1 through V7 suites run
unchanged under `satay dev`, and spill is asserted to be invisible to everything
above the store. The spill threshold is pinned at **262144 bytes (256 KiB) on the
encoded payload** (ADR-0004, H3), so the boundary test is writable. Per ADR-0011 (H3)
the integration tier drops the verbatim `satay dev`-boot and spill-decision twins,
keeping one rehydration boundary test. Blob-lifecycle is resolved by immutability
(ADR-0004/Q54): a fork shares blob references, and there is no GC or deletion in the
MVP — see the out-of-scope note below. A second `satay dev` on one data dir is refused
by the startup lock (ADR-0017/Q54) and is tested here.

### End-to-End Tests

- One `satay dev` command runs the worker, SQLite store, control and read API, and
  Studio together, and prints the local Studio URL.
- A payload over 262144 bytes (encoded) spills to a local blob file with a journal
  reference, and one at or below it stays inline.
- Spilled payloads rehydrate transparently on replay, on read, and in Studio, so
  comparison and rendering are unaffected.
- A configured sensitive field inside an over-threshold (spilled) output is still
  redacted on read — the redactor runs *after* blob rehydration, identically to an
  inline payload (ADR-0004/ADR-0014, H3).
- A booted `satay dev` supplies a working session token, and the guarded API accepts
  requests carrying it — the V5 ADR-0014 guard exercised end to end (Q43 smoke test).
- A second `satay dev` on the same `./.satay/` is refused with a clear error naming the
  holding process, protecting the single-writer journal (ADR-0017/Q54).
- No regression in V1 through V7 behavior under the unified command.

### Integration Tests

- A blob reference resolves to the same value on replay and on read.
- A fork of a run with a spilled payload keeps the source blob and its reference
  byte-for-byte unchanged — blobs are immutable, so the fork shares the reference
  (ADR-0004/Q54).

### Unit Tests

- The spill decision fires exactly at the 262144-byte encoded-payload boundary (at or
  just below stays inline; just above spills).
- A blob reference encodes and decodes symmetrically.
- The orchestrator starts and shuts down its parts in a clean order.

**Out of MVP scope (ADR-0004/Q54):** the MVP has no run-deletion path and no
compaction, so blobs are never orphaned and there is no blob garbage-collection to
test; blobs accumulate under `./.satay/` and manual removal is the escape hatch. A
future retention / `satay gc` policy must be reference-aware because forks share blobs.
Stated here so the missing coverage is deliberate, not a gap.

---

## Dependencies

- **Upstream:** V5 (control and read API), V6 (Studio), and the
  `input_ref`/`output_ref` indirection established in V1. Pulls together V1
  through V7.
- **Downstream:** none. V8 closes the MVP scope (decision D-scope).
