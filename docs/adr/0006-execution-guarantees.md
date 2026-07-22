# ADR-0006 — Execution guarantees, idempotency, and effect safety

- **Status:** Accepted
- **Date:** 2026-07-20
- **Deciders:** Jian (leejianrong2@gmail.com)

## Context

Satay must state honest guarantees rather than claim universal exactly-once
execution, which is impossible for arbitrary external systems. It must also give
developers tools to make external effects safe, and a policy for retries.

## Decision

- **Workflow replay:** a completed logical task result is normally reused during
  replay rather than re-executed (ADR-0001).
- **Task attempts:** **at-least-once physical execution with once-recorded
  logical completion.** A task may physically run more than once when completion
  is ambiguous (e.g. crash after an external effect but before the result is
  recorded). Once a successful result is durably recorded, replay reuses it.
- **Idempotency:** Satay derives a **stable idempotency key per logical task
  invocation**, stable across retries and distinct across invocations, exposed via
  `ctx.idempotency_key` for developers to pass to providers.
- **Retries:** default `retries=0`; when set, exponential backoff with jitter
  (base 1s, cap ~60s).
- **Effect safety:** project mode `effect_safety` ∈ `off` / `warn` (dev default)
  / `strict`. In `strict`, a retryable `side_effect=True` task must declare an
  idempotency or compensation strategy.

Exactly-once for external systems is **not** claimed; safety depends on
provider idempotency keys, DB transactions, transactional outbox, or explicit
compensation. Full Saga/compensation orchestration is out of scope for the MVP.

## Consequences

- The public guarantee statement (summary §8) is the wording of record.
- Task authors must design side-effecting tasks to tolerate retries, aided by the
  stable idempotency key.
- `strict` mode enforcement and the idempotency-key derivation are concrete design
  items for the worker/persistence stages.
