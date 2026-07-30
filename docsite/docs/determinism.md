# The determinism rule

**All I/O, clocks, and randomness live in tasks. Never in a workflow body.**

That is the whole rule, and it is the single most important thing to learn about Satay. Get
it wrong and replay hands you a plausible, wrong answer.

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
run_id: e3f03462d4e14bc1a89f715b1541d47f
  slow_path: really running (sleeping 20s, press Ctrl-C now)
^C
```

Now resume with the environment variable set, standing in for any outside state that moved
while the process was down:

```console
$ REPORT_FAST=1 python report.py e3f03462d4e14bc1a89f715b1541d47f
nondeterministic replay at durable-call position 0: journal expected 'slow_path' but replay issued 'fast_path' (the workflow changed between runs)
run_id: e3f03462d4e14bc1a89f715b1541d47f
  fast_path: really running
result: 21
```

Read that carefully, because two things went wrong. The runtime spotted the divergence and
said so precisely: position 0 recorded `slow_path`, the replay issued `fast_path`. And then
it carried on and returned `21`. Had the run finished uninterrupted it would have returned
`42`.

That is the default. `effect_safety` ships as `warn`, which logs and continues, because in
development a divergence is usually you editing a workflow and wanting to see what happens.

## Making it fail loudly

Set `effect_safety` to `strict` and the same divergence raises instead:

```console
$ SATAY_EFFECT_SAFETY=strict REPORT_FAST=1 python report.py b45a299b97a148ca8f85a84615b49b11
Traceback (most recent call last):
  ...
satay.replay.nondeterminism.NondeterminismError: nondeterministic replay at durable-call position 0: journal expected 'slow_path' but replay issued 'fast_path' (the workflow changed between runs)
```

Three ways to set it, highest priority first:

| Where | How |
| --- | --- |
| Per run | `satay.start(wf, n, effect_safety="strict")` |
| Per process | `SATAY_EFFECT_SAFETY=strict` |
| Default | `warn` |

`off` is the third mode. It silences the check entirely, which is worse than `warn` for no
benefit. Use `strict` anywhere a wrong answer costs more than a crash, which is most places
that are not a laptop.

!!! tip "Run strict outside development"

    `warn` is a good default for the edit-run-edit loop, and a bad default for anything that
    touches money or sends mail. Set `SATAY_EFFECT_SAFETY=strict` in those environments and
    treat a `NondeterminismError` as the bug report it is.

## What the check does not catch

`NondeterminismError` compares the **schedule** of durable calls: which task, at which
position or key. It does not compare arguments, and there is no static analysis of workflow
bodies at all.

So this goes undetected, even in `strict` mode:

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
