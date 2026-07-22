---
shaping: true
slice: V3
---

# Satay Runtime — SLICE V3: Timers and events

Adds the two durable primitives that let a workflow wait without holding a live
process (ADR-0007): durable sleep and external-event wait, plus the worker's timer
and event poll loop. This is the first slice where a run is released from memory
while waiting and later resumed by the worker, the mechanism every long-running
workflow depends on. Affordance IDs reference `BREADBOARD.md`.

---

## Affordances

| ID | Affordance | Scope in V3 |
|----|------------|-------------|
| N5 | `satay.sleep`, `satay.wait_for_event`, `satay.send_event` primitives | Full (Python-API `send_event`; the HTTP `send_event` is V5) |
| N11 | Timer and event loop: persists timer rows, polls due timers (about 1s in dev), delivers events to waiting runs and resumes them | Full |

**Deferred or unchanged:** HTTP delivery of events (N15 → V5), map/gather/child
(→ V4), Studio views (→ V6). This slice uses the deterministic clock seam from V1
to make sleeps and timeouts testable without real delay.

---

## Detailed-design items resolved in this slice

1. **Timer and event row schema.** A `timers` table (`run_id`, `timer_id`,
   `fire_at`, `kind` of `sleep` or `event_timeout`, `durable_call_identity`,
   `status`) and an `events` inbox table (optional `run_id`, `event_type`, `key`,
   `payload_ref`, `received_at`, `consumed`), so a delivered event can arrive
   **before** the workflow reaches its `wait_for_event` and still be matched.
2. **Waiting and resume semantics under replay (ADR-0001/0004).** When a workflow
   reaches `sleep` or `wait_for_event` and the journal has no resolving event, the
   worker appends `WorkflowWaiting`, releases the run (no live frame), and returns.
   On the resolving journal event the worker re-drives from the top, and the
   primitive is now a journal hit that returns. This reuses the V1 replay loop:
   waiting adds no new execution model, only new durable-call types. A graceful wake
   writes no `WorkflowResumed` and shows no ⚡, unlike a crash resume (ADR-0009/Q52);
   and if the process instead *crashes* while parked, nothing is lost, so its later
   wake is an ordinary parked wake and likewise carries no marker.
3. **Event matching key.** `wait_for_event(Type, key=…)` matches an inbox event by
   `(event_type, key)`. An unmatched inbox event persists until matched or the run
   ends.

---

## Build Plan

1. **Activate the timer and event journal events.** `TimerCreated`, `TimerFired`,
   `EventWaitStarted`, `ExternalEventReceived`, and `WorkflowWaiting` — a graceful,
   durable park that releases the run without loss. A wake from a `WorkflowWaiting`
   therefore does **not** append `WorkflowResumed` and shows no ⚡ marker; only a crash
   (a non-parked interruption) does, exactly as in V1 (ADR-0009/Q52). Define their
   payloads and their ordering relative to the durable-call identity they resolve.

2. **Store: timer and event rows.** Add the `timers` and `events` inbox tables from
   the design section to `SQLiteStore` and the `Store` interface, with queries for
   "due timers as of T" and "matching inbox event for `(type, key)`".

3. **`satay.sleep(timedelta)` (N5).** As a durable call, on a miss it appends
   `TimerCreated` and `WorkflowWaiting` with `fire_at = now + delta` computed via the
   injected clock, then releases the run. On a hit (a `TimerFired` exists), it
   returns. It survives a crash because the timer row and journal are durable.

4. **`satay.wait_for_event(Type, key=, timeout=)` (N5).** On a miss, check the inbox
   for a matching `(type, key)` event: if present, consume it, append
   `ExternalEventReceived`, and return it; otherwise append `EventWaitStarted` (and,
   if a `timeout` is given, a `TimerCreated` of kind `event_timeout`) and release the
   run. On a hit, return the recorded event or resolve the timeout.

5. **`satay.send_event(key=, event=)` (N5).** Write the event to the inbox (encoded
   via the V1 codec). If a run is currently waiting on `(type, key)`, the poll loop
   delivers it; otherwise it waits in the inbox. This is the Python-API entry point;
   the HTTP `send_event` route lands in V5 and writes to the same inbox.

6. **Timer and event poll loop (N11).** A worker background loop ticking on the
   injected clock (about 1s in dev): find due timers and append `TimerFired` (sleep)
   or resolve the wait timeout, then resume the run by re-driving replay; match
   pending inbox events to waiting runs, append `ExternalEventReceived`, and resume.
   Firing must be idempotent, so firing a timer twice does not double-resume (guard
   on the timer `status` plus journal presence).

7. **Deterministic-time tests through the seam.** With the manual clock, sleep and
   timeout fire by advancing virtual time rather than real waiting, proving both the
   durable-wait-across-crash and the timeout paths.

8. **Demo workflows.** A `sleep` workflow that goes idle then resumes; a
   `wait_for_event(ReviewDecision, key=…)` workflow resumed by `send_event`; and a
   wait with a `timeout` that fires via the timer path.

---

## Demo

A workflow calls `satay.sleep(...)`, the process goes idle (no live workflow frame),
and it resumes when the timer fires. A workflow blocks on
`wait_for_event(ReviewDecision, key=…)` and resumes when `send_event` is delivered. A
wait `timeout` fires via the timer path.

---

## Test Plan

The deterministic clock is central here: every sleep and timeout is driven by
advancing virtual time through the V1 seam, so nothing waits in real time — each
timing test below states this explicitly, and none waits on wall-clock. Per
ADR-0011 (H3) the integration tier is narrowed to the poll-loop and inbox-query
boundaries; the release-and-resume behaviour is proven observably at the E2E tier.

### End-to-End Tests

- A durable `sleep` survives across the poll interval and a crash: the run is
  released while waiting and resumes when the timer fires (virtual time advanced via
  the manual clock).
- An event wait blocks then resumes on delivery, and an event delivered before the
  wait is still matched from the inbox.
- A wait `timeout` resolves via the timer path (the manual clock advanced past
  `fire_at`).
- When a matching event and the wait's `timeout` are both due on the same tick, the
  event wins and the timeout timer is discarded (co-scheduled by advancing virtual
  time; ADR-0021).
- Multiple buffered events matching one `(type, key)` are consumed FIFO by
  `received_at` (ADR-0021).
- An unmatched inbox event persists until matched or the run ends; the disposition of
  an unconsumed event at run end is asserted (V3 design rule 3).
- All transitions are journaled: `TimerCreated`/`TimerFired`/`EventWaitStarted`/
  `ExternalEventReceived`, plus `WorkflowWaiting` (a graceful wake writes no
  `WorkflowResumed`; ADR-0009/Q52).
- A graceful `sleep`/event wake produces no `WorkflowResumed` and no ⚡ interruption
  marker, distinguishing a planned durable wait from a crash resume (ADR-0009/Q52).
- Timer firing is idempotent, with no double-resume.

### Integration Tests

- The poll loop fires only timers that are due as virtual time advances.
- The poll loop checks for a matching inbox event *before* resolving a due timeout —
  the ADR-0021 deliver-then-timeout order.
- A duplicate timer fire does not double-resume, thanks to the status and journal
  guard.

### Unit Tests

- The timer due-check compares `fire_at` against a given clock value correctly.
- The inbox match query resolves an event by `(type, key)`, returns the earliest
  match by `received_at`, and leaves non-matching events pending.
- `fire_at` is computed as clock plus timedelta.

---

## Dependencies

- **Upstream:** V1 (replay loop, journal, clock seam). Independent of V2.
- **Downstream:** V5 exposes `send_event` over HTTP into this inbox; V4's
  map/gather compose with waits; V6 renders timer and event rows in the timeline.
