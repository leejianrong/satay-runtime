# ADR-0023 — Split the code-version mismatch policy out of `effect_safety`

- **Status:** Accepted
- **Date:** 2026-07-31
- **Deciders:** Jian (leejianrong2@gmail.com)

Supersedes the "dev mode warns / strict mode may reject" wording of
[ADR-0010](0010-code-versioning.md), which read those modes off `effect_safety`. Completes
the split [ADR-0022](0022-nondeterminism-policy-split.md) started and left explicitly
unfinished. ADR-0010 remains Accepted for everything else it decides: the stamp's fallback
chain, the per-mode behaviour, the fork as the offered path, and no automatic migration.

## Context

ADR-0022 took replay divergence off `effect_safety` and named the third rider in its own
Consequences: "the code-version mismatch policy on resume (ADR-0010, `check_resume_version`)
still reads `effect_safety`. That is a third concern riding the same knob and a candidate
for the same treatment." This is that treatment.

Until now `check_resume_version(stamped, current, effect_safety)` decided, from the
effect-safety mode, whether a run resumed by a process running different code was rejected
(`strict`), warned about (`warn`), or waved through (`off`). `EffectSafety`'s docstring
documented one check — unguarded retryable side effects — so nothing told a reader that the
same variable answered a second, unrelated question.

The user-visible consequence is the same shape ADR-0022 fixed. Someone who sets
`effect_safety="off"` to quiet a warning about a side-effecting task also, silently, turns
off version-mismatch rejection. They did not ask for that, they are not told it happened,
and the setting's own documentation says it cannot happen. The converse leaks too:
tightening to `effect_safety="strict"` in CI to catch unguarded effects also starts
rejecting resumes on every commit, because in a git repo the code version is the commit
hash and it changes constantly.

Two settings, two questions:

| | Unguarded side effect | Code version changed on resume |
| --- | --- | --- |
| The question | Is this task safe to retry? | Is this run safe to resume under new code? |
| Scope | One task definition | The whole run, at one moment |
| When it fires | Every schedule of that task | Only on a crash-recovery resume |
| Who resolves it | The task author, by declaring idempotency | The operator, by forking (ADR-0004) |

## Decision

**Give the version-mismatch policy its own knob**, in the shape ADR-0022 established.

- `VersionMismatchPolicy` in `satay.config`, resolved by `resolve_version_mismatch()` with
  **override → `SATAY_VERSION_MISMATCH` → default** precedence, passed as
  `version_mismatch=` to `satay.start` and threaded to `check_resume_version`.
- `effect_safety` now governs exactly what its docstring claims and nothing else. The
  docstring says so, and names both of the settings that were split out of it.

**The default stays `warn`** — the behaviour the check already had, since it read
`effect_safety`'s `warn` default. This card makes the coupling explicit; it does not change
what the runtime does today. That is deliberate and it is *not* the same call ADR-0022 made
for divergence, because the risk profiles differ. A detected replay divergence is a present
fact: the journal and the code disagree, and continuing produces a plausible wrong answer.
A version change is not evidence that anything has diverged — the edit may not touch the
workflow's durable calls at all, and in a git repo the version changes on every unrelated
commit. Where a version change *does* cause a divergence, `NondeterminismPolicy` catches it
on its own terms, strictly, by default. Rejecting every resume across a commit boundary
would make the common local-development loop — crash, edit, re-run — fail by default while
adding no detection the strict nondeterminism check does not already provide.

`VersionMismatchPolicy` is a **distinct enum**, not a reuse of `EffectSafety`'s or
`NondeterminismPolicy`'s members, for exactly the reason ADR-0022 gave: with three identical
`off`/`warn`/`strict` vocabularies, reuse lets a swapped argument type-check as correct at
every plumbing site, which is the bug class the split exists to prevent. Parsing stays in
the one generic `_parse_mode` helper, so the duplication is three enum members and a
default.

The policy is threaded through every site that already carries `nondeterminism` — the run
controller, `build_run_handle`, the replay engine (for child runs), `apply_command`,
`apply_fork`, and the timer/event worker — so a child run cannot inherit a different default
from its parent.

## Consequences

- **Not breaking.** Every default is what it was; a caller passing nothing sees identical
  behaviour. Only a caller who was using `effect_safety` to steer the version check needs to
  move to `version_mismatch=`, and that coupling was undocumented.
- The two directions of the split are now pinned by tests: `effect_safety="off"` no longer
  disables version rejection, and `version_mismatch="off"` no longer silences the
  effect-safety warning.
- Three knobs where there was one. Accepted, for the third time and the last: they were
  never one concept, only one variable. `effect_safety` now has no remaining riders — the
  split that ADR-0022 opened is closed.
- **The `warn` default is preserved, not endorsed.** Whether resuming across a code change
  should reject by default is a separate question from whether it should be its own setting,
  and answering it here would have hidden a behaviour change inside a refactor. With the
  knob in place, that argument can now be had — and lost or won — on its own card.
- `satay dev` and the HTTP control plane construct their worker without passing any policy,
  so they use the constructor defaults and `SATAY_VERSION_MISMATCH` does not reach them.
  That gap is pre-existing and identical for `effect_safety` and `nondeterminism`; it is not
  introduced here.
