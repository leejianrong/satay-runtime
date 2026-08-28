# ADR-0034 — `satay.diff`: where two runs differ, as paths computed before redaction

- **Status:** Accepted
- **Date:** 2026-08-27
- **Deciders:** Jian (leejianrong2@gmail.com)

Completes the third leg of [ADR-0025](0025-positioning-agents-first.md)'s debugger wedge —
fork, replay, **call-by-call compare** — after [ADR-0028](0028-fork-from-code-input-override.md)
(fork from code) and [ADR-0033](0033-reading-a-run-without-forking.md) (read a run without
forking), whose terms this follows. Extends [ADR-0009](0009-local-surfaces.md)'s read-time
redaction rule with the one case where a read must be computed *before* the redactor, and
depends on [ADR-0029](0029-write-time-redaction.md) for the case that cannot be rescued.
Does **not** touch [ADR-0022](0022-nondeterminism-policy-split.md): nondeterminism
*detection* still compares the schedule, not arguments.

## Context

ADR-0025 named the wedge as fork, replay and call-by-call compare, locally, with no
account — the thing no competitor has. Two of the three were reachable from Python. The
third was not, in two different ways:

- **From Studio**, compare existed, but its diff was four booleans computed in the browser
  (`buildCompare` in `studio/src/lib/viewmodels.ts`) using whole-value JSON equality. It
  can say a prompt changed. It cannot say *which field of it* changed — which, for a
  developer comparing an agent run to its fork, is the actual question.
- **From Python**, there was nothing. `examples/fork_and_compare_demo.py` hand-rolls its
  own output comparison, because no API offered one — the same signal that produced
  ADR-0033.

Compare also had a blind spot worth fixing while here: `_calls_view` is built on
`_scan_tasks`, which reads only the four `TASK_*` events, so **`start_child` calls never
appeared in a compare row at all**. A fork that diverged inside a child workflow showed as
no difference whatsoever.

And there is a genuine correctness trap. A diff has three possible homes and two of them
are wrong:

| where | problem |
|---|---|
| in the browser / in `ReadAPI` (**after** redaction) | two *different* secrets are both `***REDACTED***`, so the diff reports them identical. A confident wrong answer. |
| in `views` (**before** redaction) carrying values | the diff payload leaks the cleartext it was built from, and trips `test_redaction_strips_secrets_on_every_read_endpoint`. |
| in `views`, **before** redaction, emitting only *paths* | correct, and discloses nothing. |

## Decision

**1. `satay.diff(run_id, other_run_id, *, store=None, redactor=None) -> RunDiff`**, in the
core, on ADR-0033's terms: reads only, nothing re-executes, redacted by default with a
`redactor=` override, values decoded but untyped. `RunDiff` carries
`calls: tuple[CallDiff, ...]`, each with the two `RecordedCall` sides — reusing ADR-0033's
type, so a diff is also a read — plus a `ValueDiff` for the arguments and for the output.

**2. The structural comparison lives in `satay/valuediff.py`, in the core.** Both the HTTP
compare view (A7/A8) and the core entry point (A1) need the same algorithm, and shared
logic belongs at the bottom where both reach down to it. This is the arrangement ADR-0029
chose for the redactor, for the same reason; it is also the alternative ADR-0033 deferred,
now taken where it actually applies.

**3. The diff is computed before redaction and emits only paths, never values.** That is
what makes it both correct and safe: two different secrets are correctly reported as
differing, and the path discloses nothing, because the redactor preserves mapping *keys*
and masks their values — so a path built from those keys names something the response
already carried.

**4. A value masked in the journal itself is reported as `redacted`, never as identical.**
With write-time redaction on (ADR-0029) the cleartext is gone at every layer, so no
comparison is possible. `ValueDiff.redacted` says equality is *unknown*; an honest "cannot
compare" beats a confident wrong answer. Read-time redaction cannot set this flag, because
the comparison runs before it.

**5. Paths use jq's spelling** — `.style`, `[1].topic`, and `.` for the whole value when
the difference is not localisable (a scalar, or two sides of different shapes). A
vocabulary readers already know, rather than a third one invented here; the repo had no
existing term for a path inside a value. **For a call's arguments the top-level index *is*
the positional argument index**, because keyword arguments are never journaled — so
"argument-level" can only ever mean "by index", never "by name".

**6. Paths are capped, and the cap is reported.** 50 paths, depth 8. Recorded values are
unbounded (payloads spill to blob files past 256 KiB) and Studio re-polls compare every
couple of seconds, so an uncapped deep walk of two large, wholly different structures is a
real cost. `ValueDiff.truncated` says the list is a prefix of the truth.

**7. A length change is reported at the node, not per element.** Pairing index-by-index
after an insertion marks every later element changed, which is noise, not information.

**8. Timing is excluded from `changed`.** Duration varies between runs for reasons that are
not a divergence; counting it would mark every call changed and make the signal worthless.
`duration_changed` is still reported separately. This keeps the meaning Studio's UI copy
has always given the word.

**9. Compare now includes child workflows**, via a shared `_calls_with_children` used by
both `run_calls` and `_compare_side`, so the two cannot disagree about what counts as a
durable call. The child's own recorded output is read from the child's journal.

**10. The compare view's `diff` field is additive**, per ADR-0018. Every existing test
asserts membership or named fields and passes untouched.

## Consequences

- **Two diff implementations coexist, deliberately and temporarily.** Studio keeps its
  client-side booleans until the frontend half lands; this PR is backend-only, so nothing
  ships inconsistent today. When Studio moves onto the server field it should delete
  `sameJson` rather than keep both.
- **Python `==` and `JSON.stringify` equality disagree**, and the two implementations
  inherit the difference. Python treats `True`/`1` and `2`/`2.0` as equal and ignores
  mapping key order; the client's JSON comparison does not. The Python answers are the
  better ones — key reordering is not a semantic change — which is a further reason for
  Studio to switch rather than for this to match the client.
- **Type-aware diffing is impossible here, and not for a fixable reason.** `views` decodes
  an already-decoded payload, which flattens `TaggedDict` and drops the `satay_type`
  discriminator; `decode` has already collapsed enums to their raw values. So "a `Ticket`
  became an `Order`" and "`Tier.GOLD` became the string `gold`" are both invisible.
  Recovering it means stopping the second decode, which changes the value shape every
  existing consumer of the read views receives — a separate decision.
- **Paths can disclose a mapping key.** If a secret is used as a *key* rather than a value,
  the path names it. This is not new: the redactor masks values whose key matches, leaving
  keys visible, so such a key is already in the response. Worth knowing, not worth blocking.
- `satay`'s public surface grows by four names (`diff`, `RunDiff`, `CallDiff`, `ValueDiff`).
  Justified by the same rule as ADR-0033: a public function's return types must be nameable
  without importing internals.
- The demo can drop its hand-rolled comparison in favour of the API, which is the ADR-0025
  demo this feature exists to make legible.

## Alternatives considered

- **Compute the diff client-side only** — rejected: it inherits the redaction false
  negative with no way to fix it from the browser, and leaves the Python API empty, which
  is the gap the demo already demonstrates.
- **Compute it in `ReadAPI`, after redaction** — rejected for the same false negative, and
  it would place shared logic above the layer that needs it.
- **Emit before/after value pairs alongside the paths** — rejected: it leaks the cleartext
  the diff was computed from, and makes the payload unbounded.
- **Report a length change per element** — rejected: one insertion becomes N differences.
- **Include timing in `changed`** — rejected: every row would be changed.
- **Extend nondeterminism detection to compare arguments** — rejected, and out of scope by
  ADR-0022, which scopes detection to the schedule on purpose. This is a *read* over two
  finished journals, not a policy that can stop a run.
- **Ship the Studio rendering in the same change** — deferred: it requires rebuilding the
  three committed bundle files under `src/satay/_studio_assets/`, and this environment's
  Node/pnpm do not match the versions CI pins. Regenerating a checked-in artifact on a
  mismatched toolchain is a worse risk than a follow-up PR.

## Refinement (Studio wiring landed, 2026-08-28)

The follow-up PR this ADR deferred has shipped (`205e568`, ADR-0034 wiring PR). Two things
this ADR's original text still describes as pending are done:

- **The Alternatives entry above** ("Ship the Studio rendering in the same change —
  deferred") is resolved: Studio's Compare view now renders `satay.diff`'s server-computed
  `RowDiff`/`ValueDiff` fields directly.
- **The Consequences bullet** "Two diff implementations coexist, deliberately and
  temporarily... Studio keeps its client-side booleans until the frontend half lands" no
  longer holds. `buildCompare` (`studio/src/lib/viewmodels.ts`) reads `row.diff` from the
  read API; the client-side `sameJson` whole-value-equality function it names is deleted,
  not merely superseded. One diff implementation exists, in `satay/valuediff.py`, as
  Decision 2 always intended.

Nothing else in this ADR is affected — Decisions 1–10 and the remaining Consequences and
Alternatives entries describe the shipped behaviour accurately.
