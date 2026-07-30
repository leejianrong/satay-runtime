# The five primitives

Beyond `@task` and `@workflow`, Satay gives you five durable operations. Each one is a
durable call, so each one is recorded, replayed, and survives a crash.

| Primitive | For |
| --- | --- |
| [`satay.sleep`](#sleep) | Waiting a fixed duration without holding a process open |
| [`wait_for_event` / `send_event`](#wait_for_event-and-send_event) | Blocking on something outside the run |
| [`satay.map`](#map) | Fanning out one task over many items |
| [`satay.gather`](#gather) | Awaiting several different durable calls at once |
| [`satay.start_child`](#start_child) | Running a linked child workflow |

All five must be called from inside a `@satay.workflow` body. Called anywhere else they raise
`RuntimeError` telling you so. `send_event` is the exception: it is a control-plane write, so
you call it from outside a workflow, which is the whole point of it.

## Running the worker

Two of these primitives park the run. When a workflow hits `satay.sleep` or `wait_for_event`,
Satay records a timer or an event wait, gives up the coroutine entirely, and marks the run
`waiting`. Something else has to wake it: the timer and event poll loop.

`await handle.result()` on a parked run returns `None` immediately rather than blocking, so a
script that uses `sleep` or `wait_for_event` needs a poll loop running alongside it:

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

!!! note "This reaches below the documented public surface"

    `TimerEventWorker`, `SQLiteStore`, and `resolve_data_dir` are not part of the eleven names
    re-exported from `satay`, so they are more likely to move than `@task` or `satay.map`. The
    runtime ships no higher-level "run my app" entry point yet, and this is the pattern the
    repository's own example code uses. Pass the same `store=` to both the worker and every
    `satay.start` call so there is one writer.

`satay dev` also runs a poll loop, but it cannot drive *your* workflows: it never imports your
module, so its registry has nothing in it. Treat `satay dev` as an inspector over the journal,
not as an application server. See the [Studio page](studio.md).

## `sleep`

```python
async def sleep(duration: float | timedelta) -> None
```

Durably waits. On the first pass it records a `TimerCreated` event and a timer row, then parks
the run. When the timer comes due the worker appends `TimerFired` and re-drives the workflow,
which replays past the completed sleep instead of sleeping again.

```python
from datetime import timedelta

@satay.workflow
async def trial(user_id: str) -> str:
    await send_welcome(user_id)
    await satay.sleep(timedelta(days=14))
    return await send_upsell(user_id)
```

A bare `float` is seconds, so `satay.sleep(2)` and `satay.sleep(timedelta(seconds=2))` are the
same thing. Fourteen days is a fine argument: the process does not need to stay up, because the
only durable state is a row with a due time.

Never use `asyncio.sleep` in a workflow body for this. It holds the frame, it is invisible to
the journal, and it sleeps in full again on every replay.

## `wait_for_event` and `send_event`

```python
async def wait_for_event(
    event_type: type | str,
    *,
    key: str | None = None,
    timeout: float | timedelta | None = None,
) -> Any

async def send_event(event, *, key=None, run_id=None, store=None) -> None
```

`wait_for_event` parks the run until a matching event arrives. Matching is on the pair
`(event_type, key)`, where the type name is derived as `module.qualname` for a class, or used
verbatim if you pass a string. Both sides have to produce the same string, so pass the same
class on both ends.

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class Approval:
    approved: bool

@satay.workflow
async def review(order_id: str) -> str:
    decision = await satay.wait_for_event(Approval, key=order_id, timeout=timedelta(days=2))
    if decision is None:
        return "timed out"
    return "approved" if decision.approved else "rejected"
```

Delivery from anywhere else:

```python
await satay.send_event(Approval(approved=True), key=order_id)
```

Four behaviours worth knowing.

An event delivered **before** the wait still matches. It sits in a persistent inbox until
something waits on that pair, so you cannot lose a race by sending too early.

With a `timeout`, the wait resolves to `None` if nothing arrives in time. Without one it waits
indefinitely.

If an event and its timeout come due on the same tick, the **event wins**. Delivery is
processed before timers on every poll iteration, deliberately.

Buffered matches are consumed first-in-first-out by arrival time, so two sends on the same
pair reach two waits in order.

The HTTP route `POST /runs/{run_id}/events` writes to the same inbox, so Studio's "send event"
and `satay.send_event` are the same operation.

## `map`

```python
async def map(task, items, *, key, concurrency=8) -> list
```

Fans one task out over many items. Each item is an independently keyed durable call, so a
crash halfway through resumes with the finished items reused and only the unresolved ones
re-run. That is the behaviour that makes fan-out worth having.

```python
@satay.workflow
async def resize_all(paths: list[str]) -> list[str]:
    return await satay.map(resize, paths, key=lambda p: p, concurrency=4)
```

`key=` is required, not optional, and must return a unique non-empty string per item. Fan-out
has no stable ordinal, so this is how each item keeps its identity across a replay. A missing
or duplicate key raises `ValueError` at schedule time, before any item runs.

`concurrency` bounds how many items are in flight on the event loop, defaulting to 8. Results
come back in **input order** regardless of who finished first.

Failure is fail-fast: one item raising fails the whole `map`. In-flight siblings settle but
their results are discarded. There is no `return_exceptions` mode.

## `gather`

```python
async def gather(*awaitables) -> list
```

Awaits several durable calls concurrently and rejoins their results **positionally**, in
argument order. Members can be heterogeneous: plain task calls, nested `map` calls, and
`start_child` calls all work together, each keeping its own identity.

```python
@satay.workflow
async def dashboard(user_id: str, order_ids: list[str]) -> list:
    return await satay.gather(
        load_profile(user_id),
        satay.map(load_order, order_ids, key=lambda o: o),
        satay.start_child(recompute_stats, user_id),
    )
```

Same fail-fast rule: one failed member fails the whole `gather`.

## `start_child`

```python
async def start_child(workflow, workflow_input=None, *, key=None) -> RunHandle
```

Starts a linked child workflow with its own run id and its own journal. The parent records
`ChildWorkflowScheduled`; the child records the reverse link, so the tree is recoverable from
either end and Studio can draw it.

```python
@satay.workflow
async def parent(order_id: str) -> str:
    handle = await satay.start_child(fulfil, order_id, key=f"fulfil-{order_id}")
    return await handle.result()
```

`await handle.result()` inside the parent yields the child's result, and on parent replay that
becomes an ordinary durable-call hit. A child interrupted mid-flight **resumes** on the parent's
resume rather than starting over, because it has its own journal. A failed child raises into the
parent.

Pass `key=` to give the child call an explicit identity. Without it the child is identified by
ordinal, with the same reordering fragility as any other unkeyed call.

## All five together

This script exercises every primitive against a real journal. It runs from a plain
`pip install satay` with no extras.

```python title="primitives.py"
import asyncio
from dataclasses import dataclass

import satay
from satay.config import db_path, resolve_data_dir
from satay.journal.store import SQLiteStore
from satay.timers import TimerEventWorker


@dataclass(frozen=True)
class Approval:
    approved: bool


@satay.task()
async def double(n: int) -> int:
    return n * 2


@satay.workflow
async def paced(n: int) -> int:
    first = await double(n)
    await satay.sleep(2)
    return first + 1


@satay.workflow
async def review(n: int) -> str:
    decision = await satay.wait_for_event(Approval, key="order-42", timeout=30)
    if decision is None:
        return "timed out"
    return "approved" if decision.approved else "rejected"


@satay.workflow
async def fanout(ns: list[int]) -> list[int]:
    return await satay.map(double, ns, key=lambda n: f"item-{n}")


@satay.workflow
async def combined(n: int) -> list:
    return await satay.gather(
        double(n),
        satay.map(double, [1, 2], key=lambda i: f"g-{i}"),
    )


@satay.workflow
async def child(n: int) -> int:
    return await double(n)


@satay.workflow
async def parent(n: int) -> int:
    handle = await satay.start_child(child, n, key="child-1")
    return await handle.result() + 1


async def finish(handle: satay.RunHandle) -> object:
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
        print("map:       ", await satay.start(fanout, [1, 2, 3], store=store).result())
        print("gather:    ", await satay.start(combined, 5, store=store).result())
        print("start_child:", await satay.start(parent, 4, store=store).result())

        print("sleep:     ", await finish(satay.start(paced, 10, store=store)))

        pending = satay.start(review, 0, store=store)
        print("waiting:   ", await pending.result(), "->", await pending.status())
        await satay.send_event(Approval(approved=True), key="order-42", store=store)
        print("event:     ", await finish(pending))
    finally:
        worker.stop()
        loop.cancel()
        store.close()


asyncio.run(main())
```

```console
$ python primitives.py
map:        [2, 4, 6]
gather:     [10, [2, 4]]
start_child: 9
sleep:      21
waiting:    None -> waiting
event:      approved
```

The `waiting: None -> waiting` line is the parked-run behaviour in the open: `result()` gave
back `None` because there was no outcome yet, and `status()` confirmed why.
