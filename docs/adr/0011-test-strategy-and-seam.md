# ADR-0011 — Test strategy and primary seam

- **Status:** Accepted
- **Date:** 2026-07-20
- **Deciders:** Jian (leejianrong2@gmail.com)

## Context

Satay is greenfield, so the first behavior tests set the pattern all later tests
follow. The property that most needs proving — crash recovery via replay
(ADR-0001) — is only observable end to end, and it depends on being able to
simulate a crash at a precise point and to control time without real waiting.
Testing replay internals directly would couple tests to implementation and defeat
the "test external behavior" principle.

## Decision

The **primary (and highest) test seam is the public API** — `satay.start`, the
decorators, and the run handle — driving real workflows against a **temporary
SQLite store**, with two injected controls:

1. a **fault-injection hook** that terminates / simulates a worker crash after a
   chosen journal event (e.g. after `TaskCompleted` for the first task);
2. **deterministic control over time and timers**, so `sleep` and timeouts are
   testable without real delay.

Tests assert on **observable outcomes** — the returned result, run status, and the
recorded journal — never on private replay internals. The headline test is the
two-task crash-recovery slice (V1). Studio is verified through its JSON read API,
not UI rendering, in the MVP.

## Consequences

- One well-defined seam keeps tests decoupled from internals and portable across
  future persistence backends.
- The runtime must expose the fault-injection and deterministic-time hooks as
  first-class test affordances (not ad-hoc monkeypatching).
- Reuse-vs-re-execution is verified via an execution-count / side-effect marker in
  test tasks, since "reused" is otherwise invisible from outputs alone.
- Establishes the pattern future slices' tests conform to (no fishing for prior
  art that cannot exist in a greenfield repo).

## Refinement (H3 test audit, 2026-07-22)

The H2 audit (TESTING.md) resolved four points about how this seam is applied:

- **Integration tier is boundary-only (Q40).** The public-API E2E tier is the primary
  coverage. The integration tier is reserved for tests that isolate a component boundary
  the E2E tier cannot reach (store `seq` allocation, codec, identity resolver, backoff
  schedule, event inbox matching, poll loop, redactor). Integration tests that merely
  restate an E2E test one level down are dropped rather than kept as mirrors.
- **Assert observable, not internals (Q41).** "Reused vs re-executed" and similar facts
  are asserted through the execution-count marker plus the journal, never by spying on
  whether the executor was invoked. There is no sanctioned internal-spying exception.
- **Seeded RNG is a determinism control alongside the manual clock (Q46).** Backoff
  jitter is runtime randomness; the manual clock pins time but not the RNG. The runtime
  therefore exposes an **injected, seedable RNG** as a first-class test affordance (real
  by default, seeded in tests), a sibling of the clock, so backoff schedules are exactly
  reproducible.
- **Fault-injection also covers a stalled worker (Q51).** The hook can pause/stall the
  worker (not only abort after an event), so a test can prove the ADR-0012 property that
  reads keep returning while the sole writer is blocked mid-write.
