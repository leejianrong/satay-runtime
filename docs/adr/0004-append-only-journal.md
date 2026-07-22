# ADR-0004 — Append-only journal as the single source of truth

- **Status:** Accepted
- **Date:** 2026-07-20
- **Deciders:** Jian (leejianrong2@gmail.com)

## Context

Satay needs one authoritative representation of run state that serves recovery,
inspection, and forking. The alternatives — a mutable shared state object, or
periodic opaque snapshots — obscure history and couple state to code. The product
thesis calls for append-only history, honest execution records, and a debugger
that shows exactly what happened.

## Decision

Each run has an **append-only, immutable, JSON-compatible journal** of events
(e.g. `WorkflowCreated`, `WorkflowStarted`, `TaskScheduled`, `TaskAttemptStarted`,
`TaskAttemptFailed`, `TaskCompleted`, `TimerCreated`, `TimerFired`,
`EventWaitStarted`, `ExternalEventReceived`, `WorkflowWaiting`, `WorkflowResumed`,
`WorkflowCompleted`, `WorkflowFailed`, `WorkflowCancelled`, `RunForked`). The
journal is the single source of durable workflow state; workflow views are
reconstructed from it. History is never rewritten — changing a run means creating
a **fork** from an earlier journal point, leaving the original intact.

Large payloads are inlined as JSON up to the spill threshold; above that they spill
to a blob store (local files in dev) and the journal stores a reference. The
threshold is **262144 bytes (256 KiB) on the encoded payload** (pinned exactly in
H3, Q49, so the boundary is testable; still tunable via config). The backend is
tunable. Redaction (N18) is applied **after** a reference is rehydrated, so a spilled
payload is scrubbed identically to an inline one and the spill path cannot leak a
secret.

## Consequences

- Recovery (ADR-0001) and the Studio timeline (ADR-0009) read the same log.
- Forking supports changing task impl, model, prompt, input, retry policy, or
  provider config from a chosen point without touching the original run.
- Exact journal event fields, transaction boundaries, ordering guarantees, and
  any compaction are detailed design work for the persistence-schema stage.
- No implicit pickle anywhere in the journal (ADR-0005).

## Refinement (H4 slice application, 2026-07-22)

Applying the fork and spill designs to the V7/V8 test plans left two lifecycle edges
undefined; both are resolved by the journal's immutability.

- **Fork operates only on settled runs in the MVP (Q53).** A fork is a pure copy of the
  source's *recorded* history up to the fork point — well-defined regardless of run
  status — but forking a run whose journal head is still growing pulls in a fork-point
  race and concurrent-divergence semantics with no MVP payoff (the V7 value is forking a
  *finished* run under changed code). So the MVP forks only **terminal** runs
  (`completed` / `failed` / `cancelled`) and **rejects a fork of an actively-executing
  run** with a clear error that names the run's status. The guard is a **status
  allow-list**, so widening it to quiescent `waiting` runs (a safe, natural first
  extension, since a parked run is not appending) is a one-line change later.
- **A fork shares immutable blobs; no GC or deletion in the MVP (Q54).** Because payloads
  are never rewritten, a fork **shares** the source's blob references rather than copying
  bytes, and the source stays byte-for-byte unchanged (the V7 fork property). The MVP has
  **no run deletion and no compaction**, so blobs are never orphaned and there is **no
  blob GC**; blobs accumulate under `./.satay/` and manual removal is the escape hatch. A
  retention / `satay gc` policy is post-MVP and, because forks share blobs, must be
  **reference-aware** (never "delete a blob when one referencing run goes").
