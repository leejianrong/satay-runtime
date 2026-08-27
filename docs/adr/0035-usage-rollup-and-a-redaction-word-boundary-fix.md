# ADR-0035 — `RunInspection.usage`: a run's self-reported totals, and the redaction bug it found

- **Status:** Accepted
- **Date:** 2026-08-27
- **Deciders:** Jian (leejianrong2@gmail.com)

Roadmap item 6 after [ADR-0025](0025-positioning-agents-first.md): usage has been journaled
since [ADR-0008](0008-model-observability.md) with no aggregate anywhere. Extends
[ADR-0033](0033-reading-a-run-without-forking.md)'s `RunInspection` rather than adding a new
top-level function, on the same redacted-by-default terms. Named as a local building block for
[ADR-0026](0026-license-and-hosted-journal-plane.md)'s tier-1 hosted "cost reporting" feature,
not a re-implementation of it — no pricing table, no hosted execution, nothing here changes.

## Context

`ctx.record_model_usage` (ADR-0008) has written a schemaless usage entry onto every attempt's
outcome event since V2. Reading it back has always meant summing raw entries by hand:
`journal.timeline.model_usage(events)` returns a flat list, `ReadAPI.task_detail` concatenates
one task's attempts without summing, and nothing totals a whole run, let alone the store. The
gap is named directly in the roadmap note this closes: "no aggregate anywhere." Satay Studio's
task view has promised "Model / Tokens / Estimated cost" since ADR-0008; this is the read that
promise depends on.

Scope: **per-run**, not cross-run. `inspect`/`diff` are both single- or two-run reads, and a
whole-store rollup (`sum across every run` — genuinely new, not an extension of any existing
query) is a different-shaped feature the roadmap note does not disambiguate for. Building it on
speculation would be the kind of public-surface growth the project has been pushing back on
after two PRs already added seven names. A per-run total is the minimal useful answer, and it is
what a cross-run rollup would sum in turn, so nothing here is thrown away by building that later.

**A real bug surfaced while building this.** `DEFAULT_REDACTION_PATTERNS` includes `"token"`,
matched as a raw substring — which also matches the plural `"tokens"`. Every self-reported
`input_tokens` / `output_tokens` field therefore already got silently masked to
`"***REDACTED***"` on any read that goes through `Redactor.redact()` (`ReadAPI.task_detail` in
particular — the one HTTP endpoint that renders usage today). The existing test for it,
`test_task_detail_groups_attempts_input_output_and_usage`, calls `views.task_detail` directly
and never exercises `ReadAPI`'s redaction pass, so nothing caught it. A usage rollup built on top
of that pattern set would report `{}` or crash summing a string — the bug had to be fixed for the
feature to mean anything.

## Decision

**1. `RunInspection` gains a fifth field: `usage: Mapping[str, int | float]`.** Not a new
top-level function or dataclass — `RunInspection` is already "what one run recorded," and a run's
usage total belongs beside its output and error as another run-level fact. Zero new names on the
public surface; `usage` is documented on the existing, already-exported type.

**2. The total sums every numeric field across every self-reported entry, under its own key** —
`input_tokens`, `output_tokens`, and whatever else a caller passes as `**extra` (a `usd` cost,
say), each summed independently. This follows ADR-0008's own framing directly: "a generic
usage/cost slot, not a model-specific schema." Hard-coding token-only totals would quietly
contradict the schema the slot was designed to be. A non-numeric field (`model`, or anything a
redactor masks to a string) is left out of the totals rather than folded in as zero: a field that
cannot be summed is *unknown*, not zero, matching `ValueDiff.redacted`'s reasoning in ADR-0034.

**3. Failed attempts count, by default and without a knob.** Same convention as
`journal.timeline.model_usage`'s default and `task_detail`'s total (KAN-479): a retried call paid
for every answer it threw away. `inspect()`'s signature does not grow a parameter for the
narrower question (`include_failed_attempts=False`) — that reads directly from
`journal.timeline.model_usage`, and adding it here before anyone has asked is exactly the
surface growth this project is watching for.

**4. Computed in `satay.control.views.run_calls`, redacted by the same pass `inspect()` already
runs.** `_usage_totals(events)` sums the raw entries and rides into the same view dict `calls` /
`output` / `error` already occupy; `inspect()`'s existing `(redactor or Redactor()).redact(view)`
call redacts it too, with no second pass to keep in sync. This is the same placement ADR-0033
chose for `_calls_view`: shared logic sits in `views`, `inspect()` wraps it.

**5. The redaction word-boundary fix.** `Redactor.matches` now splits both the field name and
each configured pattern on non-alphanumeric characters (`_`, in practice) and matches a
**contiguous run of whole words**, not a raw substring. `"token"` matches `"access_token"`
(`["access", "token"]`) but not `"input_tokens"` (`["input", "tokens"]` — plural, a different
word). Every existing default-pattern test keeps passing unchanged (verified: `password`,
`API_KEY`, `AccessKey`, `session_token`, `authorization` still match; `key`, `code_version`,
`event_id`, `identity`, `run_id`, `ordinal` still do not) — the fix removes a false positive, it
does not narrow real protection. A caller who *wants* `input_tokens` masked still gets it, by
configuring `"tokens"` or `"input_tokens"` as an explicit custom pattern; only the *default* set
stops over-matching.

**6. `inspect()` also drops any total a caller's own redactor masks**, defensively, at the
`RunInspection` construction boundary — a masked entry comes back as the string
`"***REDACTED***"`, and `_redacted_usage` filters it out rather than let a non-numeric value
sit in a field typed `Mapping[str, int | float]`.

## Consequences

- `satay`'s public surface is unchanged in name count — one field on an existing, already-public
  dataclass. No new ADR-0033/0034-style entry needed in `tests/unit/test_public_surface.py`,
  which asserts names, not fields.
- **The redaction fix is a real behavior change**, not purely additive: any caller depending on
  `input_tokens` / `output_tokens` being masked by the *default* pattern set (unlikely — nothing
  in this codebase did) now sees the real numbers. `ReadAPI.task_detail` over HTTP starts
  returning real token counts instead of `"***REDACTED***"`, which is a bug fix, not a new
  guarantee — ADR-0008 promised the numbers, `ReadAPI` just never delivered them.
- `examples/fork_and_compare_demo.py`'s own `billed_here()` helper still sums raw
  `journal.timeline.model_usage` entries by hand rather than calling `RunInspection.usage` — left
  as-is here to keep this change scoped to the rollup and the bug fix it required; adopting it
  there is a natural, separate follow-up.
- **Cross-run / whole-store rollup remains a deliberate gap**, the same way ADR-0004 names blob
  GC and run deletion as gaps rather than silent omissions. It is the natural next question once
  a real user asks it, and this per-run total is what it would sum.
- Usage is per-*attempt*, `RecordedCall.attempts` is a count with no attempt-level breakdown, so
  `RunInspection.usage` cannot be attributed back to one call in `calls`. Reading one call's own
  usage still means `ReadAPI.task_detail` or the pre-redaction
  `journal.timeline.model_usage(events)`. Documented on the field; not solved here.

## Alternatives considered

- **A new top-level `satay.usage(run_id, ...)` function**, matching the `inspect`/`diff`
  precedent exactly — rejected: it would be a second read of the same run, need its own dataclass
  (a third public name for a two-field concept), and the project has been pushing back on public
  surface growth without a strong reason. `RunInspection` already answers "what did this run
  record"; usage is more of that answer, not a new question.
- **Cross-run (whole-store) rollup as the v1 shape** — rejected for now: no existing query sums
  across runs, `store.list_runs()` plus a per-run event read for every run in the store is a real
  cost with no caching story yet, and the roadmap note does not require it. Deferred, not dropped.
- **Hard-code `input_tokens` / `output_tokens` (and drop the generic sum)** — rejected: contradicts
  ADR-0008's "generic usage/cost slot, not a model-specific schema" directly, and would silently
  drop a caller's own `usd` or similar extra from the one place meant to total it.
- **Leave the redaction pattern set alone and route the rollup around it** (e.g. compute totals
  from unredacted entries and skip the shared redaction pass) — rejected: it would make
  `RunInspection.usage` the one field on the type that is not actually redacted, breaking the
  invariant ADR-0033 states plainly — *every surface that emits a value slot to a consumer other
  than the workflow's own caller goes through `Redactor.redact`* — and it would leave the same bug
  live for `ReadAPI.task_detail`, which this rollup did not create and should not walk past.
- **Remove `"token"` from `DEFAULT_REDACTION_PATTERNS` entirely** — rejected: it is real
  protection for `access_token`, `refresh_token`, `csrf_token`, and a bare `token` field; the bug
  is the substring match, not the pattern's presence.
