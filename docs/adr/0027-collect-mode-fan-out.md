# ADR-0027 — Collect-mode fan-out: a survivable failure is a recorded failure

- **Status:** Accepted
- **Date:** 2026-08-16
- **Deciders:** Jian (leejianrong2@gmail.com)

Supersedes [ADR-0020](0020-composite-failure-semantics.md), whose fail-fast decision
stands as the default and whose deferral of collect mode does not. Delivers the
consequence [ADR-0025](0025-positioning-agents-first.md) recorded ("ADR-0020 is now
expected to be superseded by a collect-mode ADR"). Constrained by
[ADR-0005](0005-serialization-and-rehydration.md) (no Python class paths in journalled
data), [ADR-0004](0004-append-only-journal.md) (the journal is the source of truth) and
[ADR-0006](0006-execution-guarantees.md) (once-recorded logical completion).

## Context

ADR-0020 chose fail-fast for `map` / `gather` / `start_child` and deferred a collect
mode post-MVP, on the reasoning that fail-fast is simpler and the collect case had no
MVP payoff. Two things happened since.

**KAN-462 built a real ELT pipeline and priced the decision.** The failed run's own
journal reads `TaskCompleted: 5` beside `WorkflowFailed: 1` — 300,262 characters of
completed extract, including a 300 KB source that had already cost a blob write,
durably recorded, addressable by `(task_name, key)`, and permanently unreachable. A
failed run is terminal, so `satay.start(run_id=...)` re-raises the recorded failure, and
fork is terminal-only (ADR-0004), so a prefix fork plus a hand-written per-item retry is
the only path back to work the runtime is already holding. The runtime declines to
return work it has.

**The workaround is worse than the problem.** Nobody predicted this part. The advice in
`limits.md` was "have the task return a result-or-error union instead of raising". A task
that returns an outcome instead of raising records the quarantined item as
`TaskCompleted`. Studio shows a green run. The failure becomes application data,
invisible to the runtime, so retry policy, `effect_safety`, alerting, cost attribution
and `satay runs show` all see nothing. You do not get partial-failure semantics; you get
*no* failure semantics. The one workaround we shipped destroys the observability that is
the product's entire wedge (ADR-0025).

**And the roadmap moved.** ADR-0025 makes the first user an app developer building AI
features. "Draft N candidates, keep the best" is the shape of that work, and KAN-463
measures the same gap where each discarded sibling is a paid LLM call. This is on the
critical path for `0.1.0`, and it changes `map`/`gather` semantics, so it is expensive
to add after a stable release.

The hard part is not the API. It is what the journal has to say, because today a task
that exhausts its retries records attempts and **no terminal event** — the terminal
record is the run's own `WorkflowFailed`. That is sound only while a failed task always
kills the run. The moment a run survives one, its journal holds a task with a spent retry
budget and no verdict, and any later resume reads that as a miss and re-runs it.

## Decision

**1. Collect mode is opt-in, per call site: `return_exceptions=True` on `map` and
`gather`.** Fail-fast stays the default and is unchanged, down to the journal it writes.
The keyword name is `asyncio.gather`'s, deliberately: this is ordinary async Python with
durability underneath (FRAME), not a new error-aggregation vocabulary.

```python
outcomes = await satay.map(draft, briefs, key=lambda b: b.id, return_exceptions=True)
good = [o for o in outcomes if not isinstance(o, Exception)]
```

Results rejoin **in input order** as before; a failed slot holds an exception instead of
a value. `start_child` gets no flag of its own — a child is a single call, so `try/except`
around it already expresses everything a flag would, and it collects normally as a
`gather` member.

**2. A durable task failure that the run survives becomes a journal fact: the new
`TaskFailed` event.** It is the failure-side twin of `TaskCompleted` — one terminal
record per logical call, carrying the call identity (`task_name` plus `key` or `ordinal`)
and `{type, message, traceback}`. On replay a recorded `TaskFailed` is a **hit**: the
engine re-raises it without touching the executor. That is what stops a resumed run from
re-paying for a task that already spent its whole retry budget, and it is what keeps the
failure visible to everything that reads journals — Studio, the read API, cost reporting,
sibei-flow.

`TaskAttemptFailed` is unchanged and still records every attempt. `TaskFailed` says the
attempts are over.

**3. The recording rule is "survivable", not "asked for".** The engine records
`TaskFailed` when the failing call sits anywhere inside a collect composite — including a
fail-fast `map` nested in a collect `gather`, where the inner composite still *behaves*
fail-fast (it raises, its siblings' results are still discarded) but its failure is going
to be caught rather than end the run. Outside any collect composite nothing is recorded
and the journal is byte-identical to today's.

**4. A collected failure is always a Satay error type, never the task's own class.** A
task slot holds `satay.TaskFailedError`; a child-run slot holds the existing
`satay.WorkflowFailedError`. Both subclass `RuntimeError`, and `TaskFailedError` carries
`task_name`, `key` / `ordinal`, `error_type`, `error_message`, `traceback_str` — the same
attribute names `WorkflowFailedError` already uses.

This is the load-bearing choice and it is forced by ADR-0005. The journal stores an error
as strings: `error_type` is a class **name**, not an import path, because rehydrating an
arbitrary class named by journalled data is code loading wearing a hat. So the original
class cannot come back on replay. If the first pass handed the caller a `ValueError` and
replay handed back something else, a workflow branching on `isinstance` would take a
different path on replay — nondeterminism manufactured by the runtime, in the one product
whose pitch is that replay is faithful. Surfacing one stable type on **both** passes
removes the whole class of bug. The original exception is still chained as `__cause__` on
the pass that raised it, and its class name rides in `error_type` either way.

**5. A crash is not an outcome.** `SimulatedCrash`, `NondeterminismError` and
`EffectSafetyError` propagate out of a collect composite exactly as they do out of a
fail-fast one, cancelling in-flight siblings. A dead worker cannot honestly report on the
siblings it never finished, and the other two are dev-time faults the developer resolves
before re-driving.

## Consequences

- **`EventType.TASK_FAILED` is added to the journal event set.** That is the coupling
  surface with sibei-flow (KAN-654). The change is purely additive — no existing event,
  payload or schema version changes, and every reader in `control/views.py` and
  `journal/timeline.py` matches on a whitelist, so an unknown type falls through
  harmlessly. Readers that want to *use* it must opt in.
- **Studio does not render `TaskFailed` specially yet.** The run tree derives item state
  from `TaskScheduled` / `TaskAttemptFailed` / `TaskCompleted`, so a collected failure
  shows its failed attempts but no terminal marker, and a collect run shows green at the
  run level (correctly — the run did complete). Making the item render as terminally
  failed is a Studio card, not a runtime one.
- **`satay runs show` renders it as a bare type line**, per the ADR-0016 freeze on the V1
  event subset. Expected, not a gap.
- **The fail-fast path keeps its latent re-execution quirk.** A task that exhausts its
  retries under fail-fast still records no terminal event, so a `try/except` around a
  durable call still re-runs that call on replay. Making failure terminal *everywhere*
  would fix it, but it would change the exception type a `except ValueError:` in an
  existing workflow sees on replay — a real semantics change to code that works today.
  Deliberately out of scope; worth its own card before `0.1.0` if we want the invariant
  clean.
- **Cost 1 from KAN-462 is answered going forward, not retroactively.** Runs that already
  failed fail-fast still need a prefix fork to reach their stranded results; KAN-477
  (reading a terminal run's recorded results) is the card that fixes that direction.
- **`limits.md`'s "return an outcome union instead of raising" advice is withdrawn.** It
  is the exact anti-pattern this ADR exists to remove.
- Nothing here touches the core-dependency boundary (ADR-0013/0016): one new stdlib frozen
  event type, one `ContextVar`, one exception class.

## Alternatives considered

- **Keep fail-fast only, tell people to fork.** Rejected: this *is* the status quo the
  ELT evidence indicts, and it makes the runtime's answer to "you already have my data"
  be "yes, fork the run and write a retry loop".
- **A result-wrapper type (`ItemResult(ok=, value=, error=)`) instead of exceptions in
  the list.** Genuinely tempting — it removes the ambiguity of a task that legitimately
  *returns* an exception object, and it makes the failed case impossible to ignore.
  Rejected because it introduces a framework-specific type into the five-primitive surface
  that every caller must learn and unwrap, where `asyncio.gather(return_exceptions=True)`
  is already in every Python developer's hands. The ambiguity it avoids is vanishingly
  rare and `asyncio` has lived with it for a decade.
- **Return the task's original exception class on the first pass, a reconstruction on
  replay.** Rejected: manufactured nondeterminism, in exactly the product where that is
  unforgivable. See decision 4.
- **Record class import paths in the journal so the original exception can be rebuilt.**
  Rejected: reverses ADR-0005, and turns journal data into an instruction to import and
  construct an arbitrary class.
- **Make every terminal task failure record `TaskFailed`, collect mode or not.** The
  cleanest invariant, and it fixes the fail-fast quirk above. Rejected *for now* because
  it changes what an existing `try/except ValueError:` around a durable call catches on
  replay, which is a breaking semantics change to working code — and this ADR's mandate
  is that fail-fast is unchanged by default.
- **A `collect=True` flag name, or a separate `satay.map_settled`.** Rejected: a second
  spelling of a primitive doubles the surface, and `return_exceptions` is the name Python
  developers already know.
- **Ship collect mode on `map` only.** Rejected: `gather` has the same failure, the
  implementation is shared, and asymmetry between two primitives that sit next to each
  other in the docs is its own usability bug.

## Refinement (renderer revisit, 2026-08-19)

The consequence above — "`satay runs show` renders it as a bare type line, per the ADR-0016
freeze on the V1 event subset. Expected, not a gap" — is withdrawn. `render_timeline` now
summarises `TaskFailed` with its call identity and `error=<type>: <message>`. The reasoning is
recorded as a refinement on [ADR-0016](0016-core-dependency-boundary.md) (KAN-957), and the
short version is that this event was never on the far side of that freeze: it is the terminal
twin of `TaskCompleted`, inside a task-event family the renderer already summarised in full, and
the freeze traded CLI restraint against Studio coverage that does not exist for this type yet.

The neighbouring consequence stands unchanged: **Studio still does not render `TaskFailed`**
(KAN-867). Nothing else in this ADR is affected.
