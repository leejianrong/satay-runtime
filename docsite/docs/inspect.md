# Reading a Run

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
