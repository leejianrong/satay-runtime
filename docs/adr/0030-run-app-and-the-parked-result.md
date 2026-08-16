# ADR-0030 — `satay.run_app` and what `result()` answers for a parked run

- **Status:** Accepted
- **Date:** 2026-08-16
- **Deciders:** Jian (leejianrong2@gmail.com)

Extends [ADR-0007](0007-runtime-and-worker-model.md) (the worker owns the poll loop) and
sits under [ADR-0013](0013-packaging-and-frontend-stack.md) /
[ADR-0016](0016-core-dependency-boundary.md) (the affordance is core, not
`satay[studio]`). Motivated by [ADR-0025](0025-positioning-agents-first.md): the first
user is an app developer, and this is the first ten minutes of use. Companion to
[ADR-0028](0028-fork-from-code-input-override.md), which made `fork` reachable from
ordinary Python for the same reason.

## Context

Two of the five primitives park the run. `satay.sleep` and `wait_for_event` record a
timer or an event wait, release the coroutine, and mark the run `waiting`. A parked run
needs a poll loop to wake it — and the runtime shipped no supported way to have one
without booting the whole `satay dev` stack, which lives in the optional extra.

So the docs taught this, twice, each time with an admonition apologising for it:

```python
import asyncio

import satay
from satay.config import db_path, resolve_data_dir
from satay.journal.store import SQLiteStore
from satay.timers import TimerEventWorker


async def finish(handle: satay.RunHandle) -> object:
    """Drive a run, then wait for the worker if it parked on a timer or an event."""
    result = await handle.result()
    while await handle.status() in ("running", "waiting"):
        await asyncio.sleep(0.2)
        result = await handle.result()
    return result


async def main() -> None:
    data_dir = resolve_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    store = SQLiteStore.open(db_path(data_dir))
    worker = TimerEventWorker(store=store, interval=0.2)
    loop = asyncio.create_task(worker.run())
    try:
        ...   # satay.start(..., store=store) here
    finally:
        worker.stop()
        loop.cancel()
        store.close()
```

Four sub-module imports, none of them public surface. A `try`/`finally` the reader has
to get right. And a hand-rolled `finish()` poll helper that only exists because of the
second half of the problem:

**`await handle.result()` returned `None` for a parked run** — indistinguishable from a
workflow that returned `None` on purpose. Recovering the difference took a `status()`
call and a paragraph of prose in each place it came up. `ForkController.result()`
(ADR-0028) copied the behaviour, so by the time this was written down there were two
implementations of the same trap.

The docs author's verdict, writing the tutorial (KAN-486): a `satay.run_app(...)` "would
delete the worst code block in the docs".

## Decision

### 1. `satay.run_app()` is a public core async context manager

```python
async with satay.run_app() as store:
    print(await satay.start(trial, "u-1", store=store).result())
```

It opens the project-local journal, starts a `TimerEventWorker` poll loop over it, yields
the store, and on the way out stops the loop, awaits the cancelled task, and closes the
store it opened — in that order, on the normal path and the exception path alike.

`data_dir=` overrides the location; `store=` runs the loop over a store the caller opened
and leaves closing it to the caller; `interval=` is the poll cadence (default 0.2s, faster
than `satay dev`'s 1.0s because a script is usually waiting on it interactively); `clock=`
/ `rng=` / `injector=` and the three policy settings are the same seam `satay.start`
takes.

**Why a context manager and not `run_app(main)`.** Teardown is the actual problem — the
hand-rolled block needed `try`/`finally` and still leaked a cancelled task — and
`async with` is Python's answer to teardown. A callback form would own the event loop and
force the tutorial to teach inversion of control before it teaches durability. The
context manager composes with whatever `asyncio.run` the reader already has.

**Why core and not `satay[studio]`.** A reader meets `satay.sleep` on page two, long
before Studio. `satay.timers` is already pure Python; `run_app` imports the store and the
worker lazily inside the function, so `import satay` still pulls no FastAPI, uvicorn,
Pydantic, Typer or Click, and `tests/integration/test_import_hygiene.py` still passes.

**It does not import your modules.** `--app` module loading (ADR-0024) is `satay dev`'s
job, because that process has no other way to populate the registry. A script has already
imported its own workflows by the time it calls `run_app`.

### 2. A parked run answers `satay.PARKED`, never `None`

`await handle.result()` on a run parked with nothing to wake it returns the singleton
`satay.PARKED` (`repr` `<parked>`), not `None`:

```python
if await handle.result() is satay.PARKED:
    ...  # nothing has happened yet — the run is waiting on a timer or an event
```

### 3. With a poll loop in this process, `result()` waits instead

If a poll loop is running over the same store **in this process**, `result()` waits for it
to fire the timer or deliver the event and returns the real outcome. Inside
`async with satay.run_app()`, a workflow that sleeps reads like an ordinary `await`, and
the `finish()` helper has nothing left to do.

Detection is process-local and by store identity: `TimerEventWorker.run()` registers its
store for as long as the loop is alive, and `run_app` registers it too, before creating
the task, so a `result()` awaited immediately cannot lose the race. A worker in *another*
process (`satay dev`) is deliberately invisible — this process cannot promise that one is
alive, so it answers `PARKED` and lets the caller poll `status()`.

One drive must never wait: the poll loop's own. A control-plane `start` is applied inside
a tick, and a tick that waited for itself would hang the worker, so a `ContextVar` set for
the duration of `tick()` makes the in-tick answer `PARKED` immediately.

Both controllers go through one helper, `satay.api.run_handle.await_unpark`, so
`satay.start` and `satay.fork` cannot drift apart on this again.

## Consequences

**Breaking, deliberately, pre-`0.1.0`.** Any code that asserted
`await handle.result() is None` for a parked run now sees `PARKED`. That is every place
the old shape was load-bearing, and each one was a place a real result of `None` would
have been silently wrong. The repository's own suite needed 19 such assertions updated,
and every one of them read better afterwards.

**A parked run awaited under a running loop waits forever if nothing wakes it** — an event
nobody sends is an `await` nobody completes. `asyncio.wait_for` bounds it. This is the
price of the ergonomic default, and it is the same price every `await` charges.

**`ManualClock` plus `run_app` is not a useful combination.** The loop sleeps on the
injected clock, so it only ticks when the test advances it, while `result()` waits on real
time. Tests that compress time keep using `worker.tick()` directly and see `PARKED` —
which is exactly what `satay.testing.settle` already hands back.

**Two ways to run a poll loop, on purpose.** `run_app` for a script or a test; `satay dev`
for the full stack with Studio and the HTTP API. They share the journal, so a run parked by
one is woken by the other.

## Alternatives considered

**A `Parked` exception instead of a sentinel.** Precedent is good — `asyncio.Task.result()`
raises `InvalidStateError` when the task is not done. Rejected because parking is not an
error and is frequently the *expected* outcome of a first drive: every timer test in the
suite, and the examples, would have grown a `pytest.raises` or a `try`/`except` around a
line that is asserting normal behaviour. A sentinel keeps the run handle usable in the
same expression.

**`result()` always blocks until terminal.** Rejected: with no worker anywhere it would
hang instead of telling you the truth, and under a `ManualClock` it would deadlock against
the very `tick()` that was supposed to release it.

**Keep `None` and document it harder.** That is the status quo the card was filed against.
Documentation cannot fix a value that is genuinely ambiguous.

**Have `run_app` set the default store so `satay.start` needs no `store=`.** Attractive,
and rejected for now: an implicit ambient store is a second way for a run to pick a
journal, and the failure mode (two stores on one file, two writers) is worse than the
keyword it saves. Revisit if the keyword proves to be the thing people forget.
