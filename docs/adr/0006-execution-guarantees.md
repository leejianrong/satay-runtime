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

> **Scope narrowed by [ADR-0022](0022-nondeterminism-policy-split.md).** `effect_safety`
> governs the unguarded-side-effect check above and **nothing else**; replay divergence
> moved to a separate `nondeterminism` policy that defaults to `strict`. The modes and the
> `warn` default of `effect_safety` itself are unchanged.

> **Extended 2026-08-17 (KAN-476).** The setting now carries a **second** check, still
> about unsafe effects and still nothing else: a task that *does* declare
> `idempotent=True`, running in a run started **without** `satay.start(idempotency_key=)`.
> `ctx.idempotency_key` embeds the `run_id`, so it deduplicates retries and resumes of one
> run and not a re-trigger — the composition of both keys is what makes an effect survive
> being triggered twice. The new check **warns in `warn` and in `strict` alike and never
> raises**: a genuinely one-shot run has no start-level key and is correct, and the
> evidence that would distinguish it (a second trigger) is by construction not in this
> journal, so escalating a guess to `EffectSafetyError` would reject correct programs.
> The row-level half of the same trap — one call key covering an N-row effect — is not
> detectable at all from inside the runtime and is documented rather than checked.

Exactly-once for external systems is **not** claimed; safety depends on
provider idempotency keys, DB transactions, transactional outbox, or explicit
compensation. Full Saga/compensation orchestration is out of scope for the MVP.

## Consequences

- The public guarantee statement (summary §8) is the wording of record.
- Task authors must design side-effecting tasks to tolerate retries, aided by the
  stable idempotency key.
- `strict` mode enforcement and the idempotency-key derivation are concrete design
  items for the worker/persistence stages.
