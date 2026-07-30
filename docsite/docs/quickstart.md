# Quickstart

By the end of this page you will have killed a running workflow with `Ctrl-C`, restarted it,
and watched the task that had already finished get skipped. That is the whole product in one
sitting.

## Install

```bash
pip install satay
```

Or with [uv](https://docs.astral.sh/uv/), which is what the project itself uses:

```bash
uv venv
source .venv/bin/activate      # .venv\Scripts\activate on Windows
uv pip install satay
```

Either way you get exactly one package. `satay` declares no runtime dependencies, so a
`pip list` in a fresh environment shows `satay` and nothing else. The debugger UI and the
HTTP API are a separate opt-in:

```bash
pip install 'satay[studio]'     # adds fastapi, uvicorn, pydantic, typer
```

Check it landed:

```console
$ satay --help
usage: satay [-h] {runs,dev} ...

Satay Runtime — local-first durable execution (core CLI).

positional arguments:
  {runs,dev}
    runs      Inspect durable runs.
    dev       (studio extra) Boot the full local dev stack.
```

!!! warning "This is an alpha"

    `0.1.0a1` is the first published version. The runtime works and its suite is green, but
    the public API can still change between alpha releases and nothing is deprecated
    gracefully yet. Pin `satay==0.1.0a1` if you are building something you care about.

## The one-command version

From a clone of the repository, one target does the entire demo and then drops you into the
debugger:

```bash
git clone https://github.com/leejianrong/satay-runtime
cd satay-runtime
make demo
```

It runs a two-task workflow, kills the worker right after the first task's `TaskCompleted`
commits, resumes the same run id in a fresh worker, and proves with an execution counter
that the first task was reused rather than re-run:

```console
phase 1: starting run dc00741f0c3c4d88adc140f44d4f2e3c
phase 1: worker crashed — simulated crash after event 'TaskCompleted'
phase 1: step_one executions so far = 1
phase 1: step_two executions so far = 0

phase 2: resuming run dc00741f0c3c4d88adc140f44d4f2e3c
phase 2: final result = 4 (expected 4)
phase 2: step_one executions = 1 (REUSED, still 1)
phase 2: step_two executions = 1 (ran once)
```

Then it starts Satay Studio on the same journal so you can click through the timeline. Read
the [Studio page](studio.md) before you open the URL, because the `?token=` on the end of it
is not optional.

The rest of this page is the same story written by hand, from a plain `pip install`, with a
real interruption instead of a simulated one.

## Write a two-task workflow

Put this in `checkout.py`:

```python title="checkout.py"
import asyncio
import sys

import satay


@satay.task()
async def charge(cents: int) -> str:
    print("  charge: really running")
    return f"receipt-{cents}"


@satay.task()
async def email_receipt(receipt: str) -> str:
    print("  email_receipt: really running (sleeping 20s, press Ctrl-C now)")
    await asyncio.sleep(20)
    return f"emailed {receipt}"


@satay.workflow
async def checkout(cents: int) -> str:
    receipt = await charge(cents)
    return await email_receipt(receipt)


async def main() -> None:
    run_id = sys.argv[1] if len(sys.argv) > 1 else None
    handle = satay.start(checkout, 1999, run_id=run_id)
    print(f"run_id: {handle.run_id}")
    print(f"result: {await handle.result()}")


asyncio.run(main())
```

Three things are doing work here.

`@satay.task()` marks a function whose result is worth recording. Calling it from inside a
workflow becomes a **durable call**: the runtime checks the journal first and only executes
the body if there is no recorded result. Called from anywhere else it is an ordinary async
function, which keeps tasks unit-testable.

`@satay.workflow` marks the coroutine that orchestrates those calls. Its body is what gets
re-executed on every resume.

`satay.start(...)` creates or looks up a run and returns a handle. Nothing runs until you
`await handle.result()`. Passing `run_id=` for an existing, unfinished run resumes it
instead of starting a new one. The long `asyncio.sleep(20)` is only there to give you a
window to hit `Ctrl-C` in.

## Kill it in the middle

```console
$ python checkout.py
run_id: aeedd82980d741df9befdf9873ad3995
  charge: really running
  email_receipt: really running (sleeping 20s, press Ctrl-C now)
^C
```

Python dumps a `KeyboardInterrupt` traceback, which is the point. The process is gone with
the workflow half done. Copy the `run_id` off the first line.

A `./.satay/` directory appeared next to your script. That is the journal (a single
`satay.db` SQLite file, in WAL mode). Set `SATAY_DATA_DIR` or pass `--data-dir` to put it
somewhere else.

## Resume it

Same script, same run id:

```console
$ python checkout.py aeedd82980d741df9befdf9873ad3995
run_id: aeedd82980d741df9befdf9873ad3995
  email_receipt: really running (sleeping 20s, press Ctrl-C now)
result: emailed receipt-1999
```

`charge: really running` is missing, and that absence is the entire feature. The workflow
body ran from its first line again, but when it reached `await charge(1999)` the engine
found a recorded `TaskCompleted` at that position and returned the recorded value without
calling your function. `email_receipt` had no recorded result, so it ran for real, finished
this time, and the run completed.

If a task charges a credit card, this is the difference between charging once and charging
twice.

## Read the journal

The core CLI prints the timeline as text:

```console
$ satay runs show aeedd82980d741df9befdf9873ad3995
Run aeedd82980d741df9befdf9873ad3995 — 10 event(s)
    1  2026-07-30T19:47:04.840477+00:00  WorkflowCreated  workflow=checkout code_version=src:cb74cc01bda0d791
    2  2026-07-30T19:47:04.844887+00:00  TaskScheduled  task=charge ordinal=0
    3  2026-07-30T19:47:04.849620+00:00  TaskAttemptStarted  task=charge ordinal=0 attempt=1
    4  2026-07-30T19:47:04.853886+00:00  TaskCompleted  task=charge ordinal=0
    5  2026-07-30T19:47:04.858401+00:00  TaskScheduled  task=email_receipt ordinal=0
    6  2026-07-30T19:47:04.862877+00:00  TaskAttemptStarted  task=email_receipt ordinal=0 attempt=1
⚡   7  2026-07-30T19:47:19.425655+00:00  WorkflowResumed
    8  2026-07-30T19:47:19.447362+00:00  TaskAttemptStarted  task=email_receipt ordinal=0 attempt=2
    9  2026-07-30T19:47:39.472772+00:00  TaskCompleted  task=email_receipt ordinal=0
   10  2026-07-30T19:47:39.490775+00:00  WorkflowCompleted
```

Read it top to bottom and the story is all there. `charge` was scheduled, attempted once,
and completed. `email_receipt` was scheduled and attempted, then the log stops: that is
where you pressed `Ctrl-C`. The `⚡` marks `WorkflowResumed`, the event that says a run came
back from an interruption rather than waking up gracefully from a timer. `email_receipt`
then gets `attempt=2` on the same `ordinal=0`, because it is the same logical task making a
second physical attempt. `charge` never appears again.

`satay runs show` is deliberately frozen at this event subset. Timer, event, cancellation,
and fork events render as bare type lines. Studio is the surface that renders everything.

## Then what

- [The determinism rule](determinism.md), which is the one thing that can make replay give
  you a wrong answer. Read it before you write a third workflow.
- [Concepts](concepts.md) for what `ordinal=0` means and why it matters.
- [The five primitives](primitives.md) for sleeping, waiting on external events, and
  fanning out.
- [Studio and `satay dev`](studio.md) to click through the timeline instead of reading text.
