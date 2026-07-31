# First Steps

By the end of this page you will have killed a running workflow with `Ctrl-C`, restarted it, and
watched the task that had already finished get skipped. That is the whole product in one sitting.

If you have not installed Satay yet, the [tutorial index](tutorial/index.md#install) has the
one line you need.

## Create It

Put this in `checkout.py`:

```python title="checkout.py"
import asyncio
import sys

import satay


@satay.task()
async def charge(cents: int) -> str:
    print("  charge: running for real")
    return f"receipt-{cents}"


@satay.task()
async def email_receipt(receipt: str) -> str:
    print("  email_receipt: running for real (sleeping 20s, press Ctrl-C now)")
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

Three things are doing work.

**`@satay.task()`** marks a function whose result is worth recording. Call it from inside a
workflow and it becomes a **durable call**: the runtime checks the journal first and executes
the body only if there is no recorded result. Call it from anywhere else and it is an ordinary
async function, which is what keeps tasks easy to unit-test.

!!! warning "The parentheses are required"

    Write `@satay.task()`, not `@satay.task`. It is a decorator factory, because it takes
    `retries`, `timeout`, `side_effect`, and `idempotent`. `@satay.workflow` is the opposite:
    it takes no arguments, so write it bare.

**`@satay.workflow`** marks the coroutine that orchestrates those calls. Its body is what gets
re-executed on every resume.

**`satay.start(...)`** creates or looks up a run and returns a `RunHandle`. It is a plain
function, not a coroutine, so there is no `await` on it. Nothing runs until you
`await handle.result()`. Passing `run_id=` for an existing unfinished run resumes it instead of
starting a new one.

The twenty-second `asyncio.sleep` is only there to give you a window to press `Ctrl-C` in. In a
workflow body it would be a bug, and [The Determinism Rule](determinism.md) explains why. Here
it is inside a task, where sleeping is fine.

## Run It, and Interrupt It

```console
$ python checkout.py
run_id: 1d3015a1f1514d13b8aa930f445e7def
  charge: running for real
  email_receipt: running for real (sleeping 20s, press Ctrl-C now)
^C
Traceback (most recent call last):
  ...
KeyboardInterrupt
```

Python dumps a `KeyboardInterrupt` traceback, which is what you want. The process is gone with
the workflow half done. Copy the `run_id` off the first line.

A `./.satay/` directory has appeared next to your script. That is the journal: one `satay.db`
SQLite file in WAL mode, plus a `blobs/` directory for large payloads. Set `SATAY_DATA_DIR` or
pass `--data-dir` to put it somewhere else.

## Resume It

Same script, same run id:

```console
$ python checkout.py 1d3015a1f1514d13b8aa930f445e7def
run_id: 1d3015a1f1514d13b8aa930f445e7def
  email_receipt: running for real (sleeping 20s, press Ctrl-C now)
result: emailed receipt-1999
```

`charge: running for real` is missing, and that absence is the entire feature. The workflow body
ran from its first line again, but when it reached `await charge(1999)` the engine found a
recorded `TaskCompleted` at that position and returned the recorded value without calling your
function. `email_receipt` had no recorded result, so it ran for real, finished this time, and the
run completed.

If a task charges a credit card, this is the difference between charging once and charging twice.

## Read the Journal

The core CLI prints the timeline as text:

```console
$ satay runs show 1d3015a1f1514d13b8aa930f445e7def
Run 1d3015a1f1514d13b8aa930f445e7def — 10 event(s)
    1  2026-07-31T07:45:17.981713+00:00  WorkflowCreated  workflow=checkout code_version=src:d76ca379f15909ae
    2  2026-07-31T07:45:17.986717+00:00  TaskScheduled  task=charge ordinal=0
    3  2026-07-31T07:45:17.991449+00:00  TaskAttemptStarted  task=charge ordinal=0 attempt=1
    4  2026-07-31T07:45:17.995764+00:00  TaskCompleted  task=charge ordinal=0
    5  2026-07-31T07:45:18.000502+00:00  TaskScheduled  task=email_receipt ordinal=0
    6  2026-07-31T07:45:18.006617+00:00  TaskAttemptStarted  task=email_receipt ordinal=0 attempt=1
⚡   7  2026-07-31T07:45:20.934322+00:00  WorkflowResumed
    8  2026-07-31T07:45:20.945649+00:00  TaskAttemptStarted  task=email_receipt ordinal=0 attempt=2
    9  2026-07-31T07:45:40.957327+00:00  TaskCompleted  task=email_receipt ordinal=0
   10  2026-07-31T07:45:40.965280+00:00  WorkflowCompleted
```

Read it top to bottom and the story is all there.

1. `charge` was scheduled, attempted once, and completed.
2. `email_receipt` was scheduled and attempted, and then the log stops. That is where you
   pressed `Ctrl-C`.
3. The `⚡` marks `WorkflowResumed`, the event that says a run came back from an interruption
   rather than waking up gracefully from a timer.
4. `email_receipt` then gets `attempt=2` on the same `ordinal=0`, because it is the same logical
   task making a second physical attempt.
5. `charge` never appears again.

!!! info "`satay runs show` is deliberately small"

    It renders the events you see above and prints later ones (timers, external events,
    cancellation, fork) as bare type lines with no payload summary. Studio is the surface that
    renders everything. See [Limits](limits.md#tooling).

## Recap

You now know the shape of every Satay program:

- `@satay.task()` on the functions whose results should survive a crash, with the parentheses.
- `@satay.workflow` on the coroutine that calls them, bare.
- `satay.start(wf, arg)` to get a handle, `await handle.result()` to drive it.
- The same `run_id` to resume, which replays the body and reuses recorded results.
- `satay runs show <run_id>` to see what happened.

!!! tip "Want the same demo without typing it?"

    The repository ships it as a script, with the crash simulated rather than pressed by hand,
    so it is repeatable. See [Crash recovery](cookbook/crash-recovery.md) in the Cookbook.

## Next

[Concepts](concepts.md) explains what `ordinal=0` means, why the same task name appearing twice
does not collide, and what the journal is actually storing.
