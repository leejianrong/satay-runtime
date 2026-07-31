# ADR-0003 — Nondeterminism detection

- **Status:** Accepted
- **Date:** 2026-07-20
- **Deciders:** Jian (leejianrong2@gmail.com)

## Context

Event-sourced replay (ADR-0001) requires deterministic workflow bodies. A raw
`datetime.now()`, a data-driven branch flip, or an edited call order makes replay
reach a durable call that does not match the journal. Detection can be
(A) **runtime-only** — compare each replayed call against the journal; or
(B) **runtime + static analysis** — additionally AST-scan `@workflow` bodies at
import time to flag banned operations (time, randomness, I/O, db).

Static analysis of Python is leaky: indirection, helper functions, and aliasing
evade it, giving false confidence; it is meaningful extra build effort; and the
runtime check is still required regardless. It is a useful author-time linter,
not a correctness mechanism.

## Decision

**Runtime-only detection for the MVP.** On a replay mismatch, raise
`NondeterminismError` carrying the expected-vs-actual call for a clear message.
Policy on the error follows the safety mode: **dev = warn + offer to fork**;
**strict = hard-fail**. Static AST analysis is deferred as a possible post-MVP
linter.

> **Amended by [ADR-0022](0022-nondeterminism-policy-split.md).** The policy sentence above
> is superseded: divergence policy is no longer the shared `effect_safety` mode, and it
> defaults to **strict**, not warn. Warn-plus-fork survives as an explicit opt-in
> (`nondeterminism="warn"` / `SATAY_NONDETERMINISM=warn`). Runtime-only detection and
> everything else here stand.

## Consequences

- Correct by construction: the journal is ground truth, and all divergence causes
  are caught (including env/config/data-driven ones static analysis can't see).
- A latent nondeterminism bug may not surface until the first crash/recovery.
- `NondeterminismError` is a public error type. Its off/warn/strict *vocabulary* is shared
  with `effect_safety` (ADR-0006) and the code-version mismatch policy (ADR-0010), but per
  ADR-0022 it is a **separate setting with its own strict default** — the vocabulary is
  shared, the knob is not.
