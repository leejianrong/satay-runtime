# Reading and Comparing Runs

`satay.inspect` hands you back what a run recorded — every durable call, its arguments,
its result — without forking it and without running anything again.

```python
import satay

inspection = await satay.inspect(run_id)

for call in inspection.calls:
    print(call.identity, call.status, call.args, "->", call.output)
```

It is core, like `satay.fork` and `satay.run_app`. No `satay[studio]` extra, no server, no
account.

## What comes back

`RunInspection` describes the run; `RecordedCall` describes one durable call.

```python
inspection.run_id          # the run
inspection.workflow_name
inspection.status          # a satay.RunStatus
inspection.output          # the run's recorded output, or None
inspection.usage           # self-reported totals, e.g. {"input_tokens": 3211, "usd": 0.0192}
inspection.error           # {"type", "message", "traceback"} for a failed run, else None
inspection.calls           # every durable call, in the order it was scheduled
inspection.call("research:0")   # one call by identity, or None
```

```python
call.identity          # "research:0", or "resize:key:a.png" for a fan-out item
call.task_name
call.status            # "completed" | "failed" | "running"
call.args              # the recorded positional arguments
call.output            # the recorded return value
call.attempts          # how many physical attempts it took
call.duration_seconds
call.ordinal           # set for an ordinary call...
call.key               # ...or key, for a keyed fan-out item
call.map_group         # which `satay.map` a keyed item belongs to
```

Driving a two-task workflow and reading it back:

```python
@satay.workflow
async def dossier(brief: dict) -> str:
    findings = await research(brief["topic"])
    return await synthesize(findings, brief["style"])
```

```console
dossier  completed
output: '[sceptical] 2 findings'

  research:0        completed  attempts=1
      args   ('acme corp',)
      output ['finding about acme corp', 'a second finding']
  synthesize:0      completed  attempts=1
      args   (['finding about acme corp', 'a second finding'], 'sceptical')
      output '[sceptical] 2 findings'
```

## A failed run is described, not raised

`await handle.result()` raises `WorkflowFailedError`, which is what you want when you are
*driving* a run. When you are *reading* one, being interrupted by the answer is unhelpful,
so `inspect` reports it:

```python
inspection = await satay.inspect(run_id)
if inspection.error:
    print(inspection.error["type"], inspection.error["message"])
    failed = [c for c in inspection.calls if c.status == "failed"]
```

## What a run cost

A task that calls `ctx.record_model_usage(model=..., input_tokens=..., output_tokens=...,
**extra)` (ADR-0008) writes a schemaless usage entry onto its outcome event. `inspection.usage`
is that data summed: every numeric field, across every attempt in the run, under its own key.

```python
inspection = await satay.inspect(run_id)
inspection.usage   # {"input_tokens": 3211, "output_tokens": 640, "usd": 0.0192}
```

Nothing here is a pricing table — Satay ships no model adapters, so it has no idea what a
token costs. `usd` above is only there because a task chose to report it; sum whatever field
names your own tasks self-report. A run whose tasks never called `record_model_usage` gets
`{}`, not zeroes for keys nobody reported.

Failed attempts count. A task retried twice before succeeding was billed for the two answers
it threw away, same as `ReadAPI`'s own task-detail total (KAN-479) — there is no flag to narrow
this to successful work only, because nothing has needed that yet; drop to
`journal.timeline.model_usage(events, include_failed_attempts=False)` if you do.

`usage` goes through the same redactor as everything else. A field a caller's own pattern set
happens to match — `usage=Redactor(patterns=["cost"])` against a self-reported `cost` field,
say — is left out of the total rather than reported as `0`: a number the redactor could not see
is unknown, not zero. This is per-run, not per-call: usage rides on the physical attempt, not
the logical call, so it does not appear on `RecordedCall`. One call's own usage is
`ReadAPI.task_detail`'s job, not this one's.

## Reading is not forking

A read makes no journal entry. Nothing is appended, no run row is created, and no recorded
call runs again — so it is safe on a workflow whose tasks send email or charge cards, which
a fork is not.

It also works on a run that has not finished: an unfinished run reports the calls it has
recorded so far and a `status` of `running` or `waiting`. (`satay.fork` accepts terminal
runs only, because forking *writes*.)

An unknown run id raises `LookupError`, so catching it needs no import.

## Secrets are redacted

Every read is redacted, exactly as the read API behind Studio is. A field whose *name*
looks like a secret comes back as `***REDACTED***`:

```python
call.output      # {"session_token": "***REDACTED***", "user": "ada"}
```

The default patterns cover `password`, `secret`, `token`, `api_key`, `private_key`,
`credential`, `authorization` and a few more. Pass your own to widen or narrow it:

```python
from satay.redaction import Redactor

inspection = await satay.inspect(run_id, redactor=Redactor(patterns=["email", "ssn"]))
```

There is deliberately no way to ask for unredacted output — the absence of that path is
the guarantee.

!!! warning "Positional arguments cannot be redacted"

    Redaction matches field *names*, and a positional argument does not have one. A secret
    passed positionally to a task — `await charge(card_number)` — is recorded in the clear
    and comes back in the clear, here and in Studio alike. If that matters, either pass it
    inside a named field so the redactor can see it, or turn on
    [write-time redaction](guarantees.md#redaction) so it never reaches the journal at all.

## Values come back untyped

`inspect` gives you decoded JSON-compatible values — dicts, lists, strings, numbers — not
rehydrated dataclasses or Pydantic models, even when the task declares a return type.

That is deliberate. Typed rehydration needs the task still imported in the reading process
to find its annotation, which would make the type you get back depend on what your script
happened to import; and it cannot be combined with redaction, because redaction is a walk
over plain JSON structures. Reads stay predictable instead.

If you want typed values, `await handle.result()` on the run gives you the workflow's own
return value, rehydrated.

## Comparing two runs

`satay.diff` aligns two runs by durable-call identity and tells you **where** their values
differ — not merely that they do.

```python
forked = await satay.fork(run_id, before_task="synthesize", workflow_input=sharper_brief)
await forked.result()

result = await satay.diff(run_id, forked.run_id)

for call in result.changed:
    print(call.identity)
    if call.args and call.args.changed:
        print("  args differ at  ", call.args.paths)
    if call.output and call.output.changed:
        print("  output differs at", call.output.paths)
```

```console
synthesize:0
  args differ at   ('[1]',)
  output differs at ('.summary',)
```

That is the loop the debugger exists for: fork a run at the call that went wrong, drive the
fork, and read off exactly which field of which call the change moved. `research:0` above
does not appear — it was replayed off the journal and is identical.

Paths are jq-shaped: `.style`, `[1].topic`, and `.` when the difference is not localisable
to any field inside the value (a scalar, or two sides of different shapes). **For a call's
arguments the top-level index is the positional argument index** — `[1]` is the second
argument — because keyword arguments are never journaled.

`result.calls` is every aligned identity; `result.changed` is the subset that differs. A
call only one run made has `aligned = False` and no value diff, since there is nothing to
compare it against. Timing is reported as `duration_changed` but never counts as `changed`:
duration varies between runs for reasons that are not a divergence.

### Secrets are compared correctly, not just hidden

The comparison runs *before* redaction, and emits only paths. So two **different** secrets
are correctly reported as differing, even though both come back masked:

```python
call.output.paths            # ('.session_token',)   — they differ
call.a.output["session_token"]  # '***REDACTED***'   — but you cannot read either
```

A diff computed after redaction would have compared two identical `***REDACTED***`
sentinels and told you the runs agreed, which would be worse than telling you nothing.

The one case that cannot be rescued is [write-time redaction](guarantees.md#redaction): the
journal itself holds the sentinel, the cleartext is gone at every layer, and no comparison
is possible. That is reported honestly rather than guessed:

```python
call.output.redacted         # True — equality is unknown, not established
```

`ValueDiff.truncated` says the same thing about size: paths are capped, so a diff of two
enormous and wholly different values gives you a prefix of the truth rather than megabytes.
