# ADR-0033 — `satay.inspect`: reading a run's recorded calls, redacted, without forking

- **Status:** Accepted
- **Date:** 2026-08-27
- **Deciders:** Jian (leejianrong2@gmail.com)

Closes the KAN-477 launch blocker named in
[ADR-0025](0025-positioning-agents-first.md). Companion to
[ADR-0028](0028-fork-from-code-input-override.md) (`fork` from code) and
[ADR-0030](0030-run-app-and-the-parked-result.md) (`run_app`), which added the other two
core entry points for the same reason: the first user is an application developer, and
this is the first ten minutes of use. Extends [ADR-0009](0009-local-surfaces.md)'s
read-time redaction rule to a Python-level read, and depends on
[ADR-0005](0005-serialization-and-rehydration.md) for why the values come back untyped.

## Context

Reading back what a finished run recorded is the first thing anybody does with a durable
journal, and the public surface had no answer. What a user could do instead:

- `await handle.result()` — the *workflow's* output only. Nothing about the calls inside
  the run, and it **raises** for a failed run rather than describing the failure.
- `satay.fork(...)` — reaches the per-call data, and is what the card names as the
  workaround. It **writes** (a run row plus a journal prefix), then **re-executes** every
  durable call after the fork point, and it needs the workflow still registered in this
  process. Paying a write and a re-drive to answer a read is the wrong shape, and on a
  side-effecting task it is not merely wasteful.
- `store.read_events(run_id)` — works, and is what people actually do. It returns raw
  events and leaves the caller to group them by durable-call identity by hand. The
  repo's own `examples/fork_and_compare_demo.py` is the measure of that cost: six
  internal imports (`satay.config`, `satay.control.api`, `satay.control.views`,
  `satay.journal.events`, `satay.journal.store`, `satay.journal.timeline`) and five
  hand-rolled helpers, most of them re-implementing logic that already existed inside
  `satay.control.views`.

There was also a structural gap behind the usability one. `views._compare_side` already
assembled exactly "this run's durable calls with their recorded inputs and outputs", but
it was **private** and the only public route to it was `compare(store, run_id,
other_run_id)` — which demands a second run id. The single-run view did not exist at any
layer.

## Decision

**1. `satay.inspect(run_id, *, store=None, redactor=None) -> RunInspection`**, in the
core, returning frozen dataclasses: `RunInspection` (run identity, status, output, error,
lineage) carrying `calls: tuple[RecordedCall, ...]`. All three names are exported from
`satay`, because a public function's return type must be nameable without importing out
of an internal module (the KAN-524 rule).

**2. It is a read, so it accepts a run in any state.** ADR-0004's terminal-only rule
constrains `fork`, which writes; an unfinished run simply reports the calls recorded so
far. Nothing is appended, no run row is created, and no recorded call is re-executed.

**3. A failure is reported, not raised.** `error` carries the recorded
`{type, message, traceback}`. `_outcome_from_events` raises, which is right for
`await handle.result()` and wrong here: a caller who asked what happened should not be
interrupted by the answer.

**4. Redacted by default, with no way to ask for unredacted output.** The `Redactor` is
applied as the final transform with the default patterns, and `redactor=` substitutes a
caller's own — exactly `ReadAPI`'s arrangement. The absence of an unredacted path is the
guarantee (N18), so no `redact=False` flag exists.

The two existing unredacted Python read paths are deliberately not treated as precedent.
`handle.result()` returns the caller's own workflow value in-process, and redacting it
would break resume and `fork`'s input inheritance; `satay runs show` emits no value slots
at all, so its renderer has nothing to redact. The invariant this decision preserves:
*every surface that emits a `*_ref` value slot to a consumer other than the workflow's own
caller goes through `Redactor.redact`.*

**5. No typed rehydration. Values come back decoded but untyped**, as in every other read
view. Two independent reasons, and either alone is sufficient:

- **It would depend on process state, not on the journal.** `rehydrate` needs the task
  still registered to find its return annotation, so the same run would read back as
  different Python types depending on what the reading process happened to import. A read
  API whose result type varies with the reader's imports is not a contract.
- **It is incompatible with redaction.** `Redactor.redact` is a JSON deep-copy walk; hand
  it a dataclass or Pydantic instance and it falls through and redacts **nothing**. So
  "typed objects" and "redacted" cannot both be true. Redacting before rehydration does
  not rescue it either: a masked string does not satisfy an `int` annotation, and a masked
  `type` discriminator is already a deliberate `DecodeError`.

**6. `views.run_calls` is promoted to the public single-run builder**, and the per-call
assembly is extracted as a pure `_calls_view(events, record)` that both it and
`_compare_side` use. One assembly, not two that can drift. `compare`'s JSON is unchanged.

**7. `calls` is a list carrying `identity` as a field, never a dict keyed by identity.**
This is load-bearing, not stylistic, and it was found by running the code rather than by
reading it: `Redactor.matches` is a case-insensitive **substring** test over field
*names*, so keying by identity lets a task merely *named* `fetch_secret` match the
`secret` pattern and have its entire call record — task name, args, output, attempts —
replaced by the string `"***REDACTED***"`. `compare` is safe from this only incidentally,
because it flattens each side's calls into `a`/`b` values before the redactor sees them.

**8. Tasks and child workflows, ordered together by schedule position.** `_scan_tasks`
sees only the four `TASK_*` events, so a read built on it alone omits `start_child` calls
entirely — silently, and for one of the five primitives. Child calls therefore carry
`child_run_id` (inspect it in turn to walk down) and the child's **own** recorded output,
read from the child's journal since the parent's does not hold it. Their `input_ref` is the
single input value rather than an argument list, so it is wrapped to match the task
convention; treating the two alike would report a list-valued child input as N arguments.

**9. Schedule order, not identity order.** `_scan_tasks` already preserves first-seen
order; `compare` re-sorts alphabetically for stable two-run alignment, which a
single-run reader does not need and should not inherit.

**10. Placement: core `satay/api/inspection.py`, reaching `satay.control.views` through a
lazy import.** This follows the `fork` precedent (ARCHITECTURE §3.6, `api/fork.py`) rather
than the ADR-0029 precedent of moving shared logic down into core. The distinction is the
direction of the reach: ADR-0029's objection was to the *journal store* (A3, the lowest
layer) importing the control package, whereas `satay/api/` is the top layer and already
does this for `fork` and `RunHandle.cancel`. Moving the read-view assembly down to core
would be the larger and arguably purer change; it is a refactor of the HTTP read API, not
of this card.

## Consequences

- **The import-hygiene guard has to prove the boundary by *calling*, not importing.**
  `satay.control` is pure Python today, but it is also the package FastAPI lives in, and
  one module-level `import fastapi` added there would put a studio-only dependency behind
  a core public function without failing any import-time scan. A child-interpreter test
  now drives a run, reads it back through `satay.inspect`, and then scans `sys.modules` —
  the same shape ADR-0030 needed for `run_app`.
- **`RecordedCall.status` is a bare string** (`completed` / `failed` / `running`), which
  sits awkwardly beside KAN-524. It is deliberate: these three values are the read layer's
  per-*call* vocabulary, a narrower set than `RunStatus`, and the codebase carries two
  further un-enumerated status vocabularies (attempt-level, and the control plane's
  `cancelling` / `accepted`). Enumerating one in isolation would imply a consistency that
  does not exist. A `CallStatus` enum belongs with a sweep of all three.
- **`args` is the positional arguments, and read-time redaction cannot mask them.** The
  redactor keys on field names and a positional argument has none — true of the HTTP read
  API today too. A secret passed positionally is in the journal in clear, and write-time
  redaction (ADR-0029) is the answer, not this read path. Documented on the field rather
  than left for a user to discover.
- **`inspect` shadows the stdlib `inspect` module name** in the `satay` namespace. Accepted:
  `satay.map` already shadows a builtin for the same reason, which is that the obvious name
  is the guessable one. Modules inside `satay` that need the stdlib module keep importing it
  normally.
- **Durable calls means tasks and children, not timers or event waits.** Those occupy a
  separate `sleep#N` / `event#N` identity namespace that the read layer has never modelled
  as calls. Claiming to list "every durable call" would over-promise.
- The builder-side reader in ADR-0032 gets the API it would otherwise have hand-rolled,
  and it is coupled to a supported surface rather than to the journal's internals.

## Alternatives considered

- **Return typed, rehydrated objects** — rejected: incompatible with redaction, and the
  result type would depend on the reader's imports (decision 5).
- **A method on `RunHandle`** (`handle.calls()`) — rejected: a handle is the drive/write
  surface and implies you started the run; the common case for this is reading a run you
  did not start, from a run id.
- **Add a `GET /runs/{id}/calls` endpoint and have Python call the HTTP API** — rejected:
  it would make a core read depend on the studio extra and a running server.
- **Expose `store.read_events` more prominently and document the grouping** — rejected: it
  is the status quo, and the demo shows what it costs.
- **A `redact=False` escape hatch** — rejected: the absence of an unredacted read path is
  the property ADR-0009 and N18 exist to state.
- **Move the read-view assembly from `satay.control.views` down into core** (the ADR-0029
  shape) — not rejected on merit, deferred: it is a refactor of the HTTP read API rather
  than part of this card. If a second core consumer of the read views appears, do it then.
