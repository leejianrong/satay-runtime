---
shaping: true
slice: V5
---

# Satay Runtime — SLICE V5: Control and read API

This slice puts an HTTP surface in front of the runtime. Two things become
possible that an in-process API cannot serve: an outside caller can drive a
running workflow (start it, cancel it, deliver an event), and anything (a script,
a test, and later Studio) can read a run's history as JSON. It is the seam Studio
sits on in V6, so the JSON contract defined here is load-bearing. Affordance IDs
reference `BREADBOARD.md`; the decision of record is ADR-0009.

---

## Affordances

| ID | Affordance | Scope in V5 |
|----|------------|-------------|
| N15 | HTTP control API: `start` / `status` / `cancel` / `send_event` / `fork`, writing to the store; the worker polls | Endpoints stood up; `fork` route is created here but its full replay-from-point semantics and UI land in V7 |
| N16 | Read API: run list, timeline, tree, task/attempt detail, compare | Full JSON contract |
| N18 | Redactor: strips secrets and sensitive fields on read | Full |

The control API writes to the store and the worker picks changes up by polling,
matching how V3 already delivers events. It does not reach into the worker's
memory. `cancel()` on the run handle, deferred from V1, is wired here.

---

## Detailed-design items resolved in this slice

1. **The JSON API contract.** This is the artifact V6 depends on, so it is fixed
   here rather than improvised in the frontend. Read endpoints return
   journal-derived views, never live worker state: `GET /runs` (list with id,
   status, code version, start time), `GET /runs/{id}/timeline` (ordered events),
   `GET /runs/{id}/tree` (parent/child and map-item structure from V4 linkage),
   `GET /runs/{id}/tasks/{identity}` (logical task with its physical attempts,
   inputs, outputs, native stack trace, retry reason and delay, duration, recorded
   usage), and `GET /runs/{id}/compare?to={other}` (two runs aligned for
   comparison). Write endpoints mirror the Python API: `POST /runs`,
   `POST /runs/{id}/cancel`, `POST /runs/{id}/events`, `POST /runs/{id}/fork`.
2. **Write-then-poll semantics.** A control write appends the intent to the store
   (a command row or a journal event) and returns. The worker's poll loop, the
   same one that fires timers in V3, observes it and acts: `cancel` appends
   `WorkflowCancelled` and stops driving the run; an HTTP `send_event` lands in
   the V3 event inbox and is delivered the same way a Python-API `send_event`
   would be. This keeps one delivery path instead of two.
3. **Redaction as a read-time transform.** Sensitive values are stored as normal
   journal data and stripped on the way out by the redactor, driven by a
   configured set of field-name patterns. Redaction is applied to every read
   endpoint, so there is no path that returns a run's data unredacted.

---

## Build Plan

1. **Embed an HTTP server in the worker process.** Run it alongside the asyncio
   worker (ADR-0009): one process, the worker loop and the HTTP server sharing the
   `SQLiteStore`. Pick the concrete server library during the architecture step
   (G); the design here only assumes an async HTTP framework over the store.

2. **Control endpoints (N15).** Implement `POST /runs` (start, returning the run
   id and handle-equivalent status), `POST /runs/{id}/cancel`,
   `POST /runs/{id}/events` (send_event), and register `POST /runs/{id}/fork` as a
   route. Each writes to the store and returns without blocking on the worker.

3. **Write-then-poll wiring.** Extend the V3 poll loop to also pick up control
   writes: apply `cancel` by appending `WorkflowCancelled` and halting the run,
   and route HTTP events into the V3 inbox. Confirm a cancel takes effect within
   one poll interval and that a run cancelled mid-task settles cleanly.

4. **`cancel()` on the run handle (N4).** Wire the handle method deferred from V1
   to the cancel endpoint (or directly to the store when in-process), so both the
   Python and HTTP paths reach the same journal transition.

5. **Read API (N16).** Build the read endpoints from the contract above as pure
   functions over the journal plus the V4 tree linkage. The timeline is the
   ordered event stream; the tree is reconstructed from parent refs and map-item
   keys; task detail groups a logical task with its attempts and pulls the usage
   slot recorded in V2. Compare aligns two runs by durable-call identity.

6. **Redactor (N18).** Apply the configured field-name patterns to every read
   response as a final transform. Prove that a task recording a secret in its
   input or output has that field redacted in the timeline, task detail, and
   compare views.

7. **Fork route stub.** Register the `fork` endpoint and validate its request
   (source run, fork-from event). Creating the forked run and re-running its
   downstream is V7 work; here the route exists and rejects malformed requests so
   V7 builds on a stable surface.

8. **Demo and tests.** Start a run over HTTP; deliver a V3 approval by HTTP
   `send_event` and watch the workflow resume; cancel a run; fetch a run's
   timeline and tree as JSON and confirm configured secrets are absent.

---

## Demo

Start a run over HTTP. Deliver an approval by HTTP `send_event` and watch a V3
workflow resume. Cancel a run. Fetch a run's timeline and tree as JSON with
secrets redacted.

---

## Test Plan

Tests drive the HTTP surface and the read endpoints against a temp `SQLiteStore`
seeded through the V1 seam. Control writes are asserted through the poll loop
(virtual time advanced via the manual clock). The read-API JSON contract is treated
as **additive and forward-compatible** (ADR-0018, H3): V5 enumerates the fields V2
(usage slot), V4 (tree linkage), and V7 (version-mismatch field, `RunForked` lineage)
will add, and read-view tests assert on the fields they need while tolerating
extra/unknown ones. The compare endpoint is owned and tested here; V7 owns only the
Studio side-by-side view (ADR-0009, H3). Per ADR-0011 (H3) the integration tier keeps
only the boundary tests, dropping the middle restatements of redaction and fork
validation that the E2E and unit tiers already cover.

### End-to-End Tests

- An external caller can start, cancel, and send an event to a workflow over HTTP,
  and each takes effect through the worker's poll loop within one interval.
- Read endpoints return journal-derived views (list, timeline, tree, task/attempt
  detail, compare) matching the additive JSON contract.
- Redaction strips every configured sensitive field on every read endpoint, with
  no unredacted path.
- `cancel()` on the run handle and the HTTP cancel reach the same
  `WorkflowCancelled` transition.
- A run cancelled mid-task settles cleanly: the in-flight attempt is not left
  dangling and the run reaches `WorkflowCancelled`.
- A request with a missing or invalid session token is rejected, and a request with
  a disallowed `Origin`/`Host` is rejected (ADR-0014 guard, owned by the V5 server).
- The API refuses a non-loopback bind (ADR-0014).
- While the worker is stalled mid-write (fault-injection hook), a read endpoint still
  returns promptly — the non-blocking-reads guarantee (ADR-0012).
- The `fork` route exists and validates its request, deferring execution to V7.

### Integration Tests

- `POST /runs` starts a run and returns its id and status.
- An HTTP `send_event` lands in the V3 inbox and resumes a waiting run.
- `POST /runs/{id}/cancel` appends `WorkflowCancelled` and halts the run within one
  poll interval.
- Each read endpoint derives its view (list, timeline, tree, task detail, compare)
  from a seeded journal.
- The read API returns 404 for an unknown run and rejects a malformed write body.

### Unit Tests

- Redaction field-pattern matching flags configured field names.
- Compare aligns two runs by durable-call identity.
- Fork request validation checks the source run and fork-point event.

---

## Dependencies

- **Upstream:** V1 (journal, store), V2 (attempts and usage in task detail), V3
  (poll loop and event inbox, reused for control writes and HTTP events), V4 (tree
  linkage the tree endpoint reads).
- **Downstream:** V6 renders every one of these read endpoints; V7 builds fork and
  compare UI on the routes stood up here.
