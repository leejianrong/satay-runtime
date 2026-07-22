# ADR-0021 — Event delivery ordering and the wait_for_event timeout race

- **Status:** Accepted
- **Date:** 2026-07-22
- **Deciders:** Jian (leejianrong2@gmail.com)

## Context

`satay.wait_for_event(Type, key=, timeout=)` (V3) parks a workflow until a matching
external event arrives, optionally bounded by a timeout implemented as a timer row. The
H2 test audit (TESTING.md, Q48) found two resolutions undefined, which makes the
durable-wait behaviour non-deterministic and therefore untestable:

1. **The timeout race.** If, on the same poll tick, a matching inbox event and the
   wait's timeout are both due, which one resolves the wait.
2. **Multi-event ordering.** If several buffered inbox events match the same
   `(event_type, key)`, which one is consumed.

Durable waits are the mechanism that lets a workflow sleep for days without a live
process, so their resume rules must be predictable rather than dependent on poll-loop
iteration order.

## Decision

- **Event wins over a simultaneously-due timeout.** In one poll-loop tick, the worker
  checks for a matching inbox event **before** resolving the wait's timeout. A delivered
  event resolves the wait and the timeout timer is discarded. The timeout only fires
  when no matching event is present at or before its `fire_at`.
- **FIFO by arrival.** When several buffered events match one `(event_type, key)`, the
  earliest by `received_at` is consumed first. Remaining matches stay in the inbox for
  later waits, or until the run ends (ADR-0004; V3 design rule 3).

## Consequences

- A workflow can never lose an already-delivered approval to its own timeout firing in
  the same instant, which is the intuitive and safe outcome.
- Event delivery is deterministic and assertable through the V1 seam under the manual
  clock: tests advance virtual time to co-schedule an event and a timeout and assert the
  event wins.
- The poll loop must enforce the check-event-then-timeout order and a `received_at`
  ordering on inbox matches; both are cheap guards.
- Refines the V3 timers-and-events design; relies on ADR-0007 (poll model) and
  ADR-0004 (inbox persistence).
