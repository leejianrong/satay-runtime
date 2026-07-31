# The determinism rule

**All I/O, clocks, and randomness live in tasks. Never in a workflow body.**

That is the whole rule, and it is the single most important thing to learn about Satay. Get
it wrong and replay refuses to finish the run — or, if you have turned the check down, hands
you a plausible wrong answer instead.

## Why the rule exists

Satay has no coroutine snapshot and no stack capture. On resume it calls your workflow
function again from line one, and matches each durable call it sees against the journal by
position. A recorded result answers the call; an unrecorded one executes for real.

That only works if the second pass issues the same sequence of durable calls as the first.
Anything in the body that can differ between two executions can change that sequence: the
clock, an environment variable, `random`, a database read, an HTTP call, a `dict` you
mutate elsewhere in the process.

Tasks are exempt because a task body runs at most once per recorded result. That is the
point of a task. Put the messy part inside one.

## The shape of the mistake

```python
import os
import random
from datetime import datetime

@satay.workflow
async def report(n: int) -> int:
    if os.environ.get("REPORT_FAST"):     # (1) reads outside state
        return await fast_path(n)
    if datetime.now().hour < 9:           # (2) reads the clock
        return await overnight(n)
    sample = random.sample(rows, 3)       # (3) draws from an RNG
    return await summarise(sample)
```

Every one of those three lines belongs inside a task. The fix is mechanical: wrap the read
in a `@satay.task()` and await it, so its answer is recorded once and replayed thereafter.

```python
@satay.task()
async def read_config() -> bool:
    return bool(os.environ.get("REPORT_FAST"))

@satay.workflow
async def report(n: int) -> int:
    if await read_config():               # recorded on the first pass, replayed after
        return await fast_path(n)
    return await slow_path(n)
```

Now the branch is decided by a journal entry, not by whatever the environment happens to
say the second time around.

## Watching it break

Save this as `report.py`. The bug is on the marked line.

```python title="report.py" hl_lines="25"
import asyncio
import os
import sys

import satay


@satay.task()
async def slow_path(n: int) -> int:
    print("  slow_path: really running (sleeping 20s, press Ctrl-C now)")
    await asyncio.sleep(20)
    return n * 2


@satay.task()
async def fast_path(n: int) -> int:
    print("  fast_path: really running")
    return n


@satay.workflow
async def report(n: int) -> int:
    # WRONG: the workflow body reads the environment, so a replay can take a
    # different branch than the original run took.
    if os.environ.get("REPORT_FAST"):
        return await fast_path(n)
    return await slow_path(n)


async def main() -> None:
    run_id = sys.argv[1] if len(sys.argv) > 1 else None
    handle = satay.start(report, 21, run_id=run_id)
    print(f"run_id: {handle.run_id}")
    print(f"result: {await handle.result()}")


asyncio.run(main())
```

Start it and interrupt it while `slow_path` is sleeping:

```console
$ python report.py
run_id: 5d7301ecdd7845e98c7daa9adbb4ffbf
  slow_path: really running (sleeping 20s, press Ctrl-C now)
^C
```

Now resume with the environment variable set, standing in for any outside state that moved
while the process was down:

```console
$ REPORT_FAST=1 python report.py 5d7301ecdd7845e98c7daa9adbb4ffbf
run_id: 5d7301ecdd7845e98c7daa9adbb4ffbf
Traceback (most recent call last):
  ...
satay.replay.nondeterminism.NondeterminismError: nondeterministic replay at durable-call position 0: journal expected 'slow_path' but replay issued 'fast_path' (the workflow changed between runs)
```

The message names exactly what changed: position 0 recorded `slow_path`, the replay issued
`fast_path`. Note what is *not* in that output — `fast_path` never printed. The check fires
before the divergent call executes, so nothing was recorded and the run is still resumable
once you fix the body.

That is the default, and it has a name of its own: the **nondeterminism policy**, which
ships as `strict`.

## Opting out while you iterate

Editing a workflow and re-driving an old run is a normal thing to do at a laptop, and a
hard failure gets in the way. Set the policy to `warn` and the same divergence logs and
carries on:

```console
$ SATAY_NONDETERMINISM=warn REPORT_FAST=1 python report.py 5d7301ecdd7845e98c7daa9adbb4ffbf
run_id: 5d7301ecdd7845e98c7daa9adbb4ffbf
nondeterministic replay at durable-call position 0: journal expected 'slow_path' but replay issued 'fast_path' (the workflow changed between runs)
  fast_path: really running
result: 21
```

Stare at the last line. The run reported success and returned `21`. Uninterrupted it would
have returned `42`. That is the whole reason `warn` is not the default: a wrong answer that
reports success is indistinguishable from a right one, and the warning scrolls away.

Three ways to set the policy, highest priority first:

| Where | How |
| --- | --- |
| Per run | `satay.start(wf, n, nondeterminism="warn")` |
| Per process | `SATAY_NONDETERMINISM=warn` |
| Default | `strict` |

`off` is the third mode: same as `warn` without the log line. There is little reason to
choose it over `warn`.

!!! warning "This is not `effect_safety`"

    `effect_safety` is a separate setting covering a separate problem — unguarded
    retryable side effects — and it keeps its `warn` default. See
    [guarantees](guarantees.md#effect_safety). The two share an `off`/`warn`/`strict`
    vocabulary and nothing else; changing one does not move the other.

## What the check does not catch

`NondeterminismError` compares the **schedule** of durable calls: which task, at which
position or key. It does not compare arguments, and there is no static analysis of workflow
bodies at all.

So this goes undetected, `strict` default and all:

```python
@satay.workflow
async def pipeline(n: int) -> int:
    a = await first(n)
    b = await second(n)
    return a + b
```

Interrupt it after `first` completes, then resume the same run id with a different input.
The call schedule is identical, so nothing complains: `first` returns its recorded result
computed from the old input, `second` runs against the new one, and the run completes with
a number derived from both.

```console
$ python argdrift.py 10          # first(10) -> 20, then Ctrl-C during second
$ python argdrift.py 99 07ef98883d01428ea83ccdc20b198fc4
  second: really running with n=99 (sleeping 15s, Ctrl-C now)
result: 120
```

`120` is `20 + 100`: half from the first input, half from the second. No error, no warning.
The lesson is that the runtime check is a safety net for the common structural mistake, not
a proof of correctness. Resume a run with the input it started with.

The [limits page](limits.md) lists the rest of what detection does not do.

## The checklist

Before a workflow body ships, scan it for these. Anything you find belongs in a task.

- [ ] `datetime.now()`, `time.time()`, `time.monotonic()`. Use [`satay.sleep`](primitives.md#sleep)
      for delays, and put a timestamp you need to keep inside a task.
- [ ] `random` or `uuid4()`. Generate ids in a task, or derive them from the workflow input.
- [ ] `os.environ`, config files, feature flags.
- [ ] Any network or database call, including "just a quick read".
- [ ] `asyncio.sleep`. It is not durable: it does not park the run, and it re-sleeps in full
      on every replay. `satay.sleep` records a timer instead.
- [ ] Module-level mutable state that another part of the process writes.
- [ ] `try`/`except` around a durable call whose branch depends on something transient.

Two things that are fine in a body, and worth knowing: reading the workflow's own input
argument, and reading the result of an earlier durable call. Both are recorded, so both
replay identically.
