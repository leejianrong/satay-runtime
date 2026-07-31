# ADR-0022 — Split the nondeterminism policy out of `effect_safety`, default it to strict

- **Status:** Accepted
- **Date:** 2026-07-31
- **Deciders:** Jian (leejianrong2@gmail.com)

Supersedes the policy half of [ADR-0003](0003-nondeterminism-detection.md) ("policy on the
error follows the safety mode: dev = warn, strict = hard-fail") and narrows the
`effect_safety` scope stated in [ADR-0006](0006-execution-guarantees.md). Both remain
Accepted for everything else they decide; detection remains runtime-only, and
`effect_safety`'s modes and `warn` default are unchanged.

## Context

`effect_safety` ∈ `off`/`warn`/`strict` shipped as a single knob governing two unrelated
checks:

1. **Unguarded side effects** (A10.2, ADR-0006). A retryable `side_effect=True` task that
   declares no idempotency or compensation strategy is rejected in `strict`, logged in
   `warn`.
2. **Replay divergence** (N9, ADR-0003). A durable call whose task name does not match the
   journal at that position raises `NondeterminismError` in `strict`; in `warn` it logs and
   **lets the divergent call proceed as a fresh miss**.

The shared default was `warn`. So out of the box, a workflow whose body was edited to
reorder its durable calls logged one line and then **completed successfully with a wrong
result** — reproduced against a clean install as `21` where `42` was correct, with the run
reporting success. `EffectSafety`'s own docstring documented only concern 1, so nothing told
a reader that the same setting decided whether a divergent replay raised or returned
garbage.

The two checks have opposite risk profiles, which is why one default cannot serve both:

|  | Unguarded side effect | Replay divergence |
| --- | --- | --- |
| What it reports | A *risk*: the task may be perfectly safe, the runtime cannot tell | A *present fact*: the journal and the code disagree, here, now |
| False positives | Common — any correctly-guarded-by-hand task | None — the mismatch is observed, not inferred |
| Cost of continuing | Possibly nothing | A plausible wrong answer, reported as success |

A wrong answer that reports success is the worst failure mode a durable-execution runtime
has, because it is indistinguishable from a right one. Failing loudly is recoverable: the
divergent call never executes, nothing is recorded, and the run stays resumable once the
body is fixed.

## Decision

**Split the knob.**

- `effect_safety` keeps its documented meaning (concern 1 only) and its **`warn` default**.
  An unguarded retryable side-effecting task is a design smell worth a warning during local
  iteration, not a hard stop.
- A **separate nondeterminism policy** governs concern 2, defaulting to **`strict`**:
  `NondeterminismPolicy` in `satay.config`, resolved by `resolve_nondeterminism()` with
  override → `SATAY_NONDETERMINISM` → `strict` precedence, and passed as `nondeterminism=`
  to `satay.start`. `warn` and `off` remain available as explicit opt-ins.

`NondeterminismPolicy` is a **distinct enum**, not a reuse of `EffectSafety`'s members,
despite the identical `off`/`warn`/`strict` vocabulary. Reuse would type-check a swapped
argument as correct at every plumbing site — engine, runner, worker, command dispatch —
which is exactly the class of bug the split exists to prevent. Two enums make a mix-up a
`mypy --strict` error; the shared parsing lives in one generic helper, so the duplication is
three enum members and a default.

Every engine-construction site threads both policies, including child and forked engines: a
child inheriting a different default from its parent would reintroduce the silent-wrong-answer
path one level down.

## Consequences

- **Breaking, deliberately.** A resume that diverges now raises where it previously returned
  a wrong answer. Alpha with no users is the cheapest moment this default will ever change.
- Callers that want the old behaviour ask for it: `nondeterminism="warn"` per run or
  `SATAY_NONDETERMINISM=warn` per process.
- Two knobs to learn instead of one. Accepted: they were never one concept, only one
  variable, and the docs now describe each where it applies.
- **This does not widen detection.** The engine still compares the durable-call *schedule*,
  not arguments (ADR-0003). Resuming with a different input is still undetected under
  `strict`; strict only means the divergences that *are* detected stop the run.
- The code-version mismatch policy on resume (ADR-0010, `check_resume_version`) still reads
  `effect_safety`. That is a third concern riding the same knob and a candidate for the same
  treatment; it is out of scope here and its `warn` default is unchanged.
