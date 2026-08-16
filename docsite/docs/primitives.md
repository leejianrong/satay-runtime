# The Five Primitives

Beyond `@task` and `@workflow`, Satay gives you five durable operations. Each one is a durable
call, so each one is recorded, replayed, and survives a crash.

| Primitive | For |
| --- | --- |
| [`satay.sleep`](#sleep) | Waiting a fixed duration without holding a process open |
| [`wait_for_event` / `send_event`](#wait_for_event-and-send_event) | Blocking on something outside the run |
| [`satay.map`](#map) | Fanning out one task over many items |
| [`satay.gather`](#gather) | Awaiting several different durable calls at once |
| [`satay.start_child`](#start_child) | Running a linked child workflow |

All five must be called from inside a `@satay.workflow` body. Called anywhere else they raise
`RuntimeError` telling you so. `send_event` is the exception: it is a control-plane write, so you
call it from outside a workflow, which is the whole point of it.

## Running the worker

Two of these primitives park the run. When a workflow hits `satay.sleep` or `wait_for_event`,
Satay records a timer or an event wait, gives up the coroutine entirely, and marks the run
`waiting`. Something else has to wake it: the timer and event poll loop.

`satay.run_app()` is that loop. It opens the journal, starts the poll loop over it, and yields
the store to pass to `satay.start`:

```python
import asyncio

import satay


async def main() -> None:
    async with satay.run_app() as store:
        handle = satay.start(trial, "u-1", store=store)
        print(await handle.result())


asyncio.run(main())
```

Inside the block, `await handle.result()` on a run that parks **waits** for the loop to wake it
and returns the real result, so a workflow that sleeps for two weeks reads like an ordinary
`await`. On the way out, the loop is stopped and the store is closed for you, whether the block
ended normally or by exception.

Pass the yielded `store` to every `satay.start` and `satay.send_event` in the block. One store is
one writer, which is what keeps the journal coherent.

Three keyword arguments are worth knowing. `data_dir=` puts the journal somewhere other than
`./.satay`. `store=` runs the loop over a store you opened yourself — an in-memory one in a test,
say — and leaves closing it to you. `interval=` is the poll cadence in seconds, `0.2` by default.

Outside a `run_app` block there is no loop, and `result()` on a parked run cannot invent one. It
returns `satay.PARKED` — a sentinel, deliberately not `None`, so you can tell "nothing has
happened yet" from a workflow that returned `None` on purpose:

```python
if await handle.result() is satay.PARKED:
    print(await handle.status())   # 'waiting'
```

!!! tip "A run parked on an event nobody sends waits forever"

    That is what awaiting it means, and it is the same deal every `await` offers. Send the event
    before you await the handle, or bound the wait with
    `asyncio.wait_for(handle.result(), timeout=30)`.

`satay dev --app mypkg.workflows` is the same poll loop with the rest of the stack around it. It
imports the modules you name, which is what puts your workflows in the registry, and then runs the
loop, the store, the API, and Studio in one process. Its worker will wake runs your own scripts
parked, so the two shapes interoperate over the same journal. A bare `satay dev` with no `--app`
imports nothing and drives nothing, and it says so at boot. See
[Studio and `satay dev`](studio.md#telling-satay-dev-where-your-workflows-live).

## `sleep`

```python
async def sleep(duration: float | timedelta) -> None
```

Durably waits. On the first pass it records a `TimerCreated` event and a timer row, then parks the
run. When the timer comes due the worker appends `TimerFired` and re-drives the workflow, which
replays past the completed sleep instead of sleeping again.

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

!!! warning "Never `asyncio.sleep` in a workflow body"

    It holds the frame, it is invisible to the journal, and it sleeps in full again on every
    replay. Inside a task it is fine.

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
verbatim if you pass a string. Both sides have to produce the same string, so pass the same class
on both ends.

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

Delivery from anywhere else. Note the `await`: unlike `satay.start`, `send_event` is a coroutine.

```python
await satay.send_event(Approval(approved=True), key=order_id)
```

Four behaviours worth knowing.

An event delivered **before** the wait still matches. It sits in a persistent inbox until
something waits on that pair, so you cannot lose a race by sending too early.

With a `timeout`, the wait resolves to `None` if nothing arrives in time. Without one it waits
indefinitely.

If an event and its timeout come due on the same tick, the **event wins**. Delivery is processed
before timers on every poll iteration, deliberately.

Buffered matches are consumed first-in-first-out by arrival time, so two sends on the same pair
reach two waits in order.

The HTTP route `POST /runs/{run_id}/events` writes to the same inbox, so Studio's "send event"
button and `satay.send_event` are the same operation.

## `map`

```python
async def map(task, items, *, key, concurrency=8) -> list
```

Fans one task out over many items. Each item is an independently keyed durable call, so a crash
halfway through resumes with the finished items reused and only the unresolved ones re-run. That
is the behaviour that makes fan-out worth having.

```python
@satay.workflow
async def resize_all(paths: list[str]) -> list[str]:
    return await satay.map(resize, paths, key=lambda p: p, concurrency=4)
```

`key=` is required, not optional, and must return a unique non-empty string per item. Fan-out has
no stable ordinal, so this is how each item keeps its identity across a replay. A missing or
duplicate key raises `ValueError` at schedule time, before any item runs.

`concurrency` bounds how many items are in flight on the event loop, defaulting to 8. Results come
back in **input order** regardless of who finished first.

### Failure: fail-fast, or collect

By default one item raising fails the whole `map`. In-flight siblings settle but their results are
discarded, and the run ends in `WorkflowFailed` — native `await` semantics (ADR-0020).

Pass `return_exceptions=True` for **collect mode** (ADR-0027) when you would rather keep the
siblings than lose them — "draft five candidates, keep the ones that came back" is the shape of
work this exists for:

```python
@satay.workflow
async def draft_all(briefs: list[Brief]) -> list[str]:
    outcomes = await satay.map(
        draft, briefs, key=lambda b: b.id, return_exceptions=True
    )
    return [o for o in outcomes if not isinstance(o, Exception)]
```

Every item settles, and the returned list holds each item's result *or* its error in the item's
input position. Three things are worth knowing:

- **A failed slot always holds `satay.TaskFailedError`**, never the exception class your task
  raised. The journal stores an error as a class *name* plus a message, not an import path, so the
  original class cannot be rebuilt on replay — and a slot whose type changed between the first pass
  and the replay would be nondeterminism the runtime invented. The original exception is still
  chained as `__cause__` on the pass that raised it, and its name is in `.error_type`. Read the
  identity off `.task_name` / `.key`.
- **The failure stays visible to the runtime.** Each collected failure is recorded as a terminal
  `TaskFailed` journal event alongside its `TaskAttemptFailed` attempts, so retries, Studio and the
  read API all still see it. This is the point: a task that swallows its own errors and returns an
  outcome object records `TaskCompleted` and hides the failure from everything.
- **A recorded failure replays as a hit.** Resume a run that collected a failure and the failed item
  is *not* re-run — it raises straight back out of the journal, like a completed item is reused.

Retries are unchanged: an item fails only after its whole retry budget is spent.

!!! warning "A crash is not a collected outcome"

    Collect mode collects task failures. A worker crash, a `NondeterminismError` or an
    `EffectSafetyError` still aborts the whole fan-out and cancels in-flight siblings — a dead
    worker cannot report honestly on work it never finished.

## `gather`

```python
async def gather(*awaitables, return_exceptions=False) -> list
```

Awaits several durable calls concurrently and rejoins their results **positionally**, in argument
order. Members can be heterogeneous: plain task calls, nested `map` calls, and `start_child` calls
all work together, each keeping its own identity.

```python
@satay.workflow
async def dashboard(user_id: str, order_ids: list[str]) -> list:
    return await satay.gather(
        load_profile(user_id),
        satay.map(load_order, order_ids, key=lambda o: o),
        satay.start_child(recompute_stats, user_id),
    )
```

Same fail-fast rule by default: one failed member fails the whole `gather` and the siblings'
results go nowhere. `return_exceptions=True` collects here too, with the same semantics as `map`
above — a failed task member's slot holds `satay.TaskFailedError`, and a failed `start_child`
member's slot holds `satay.WorkflowFailedError` (the child's failure is already terminal in the
child's own journal). Both subclass `RuntimeError`.

The mode belongs to the composite you set it on, not to the whole workflow: a plain `map` nested
inside a collecting `gather` still fails fast and still discards its own siblings — the `gather`
just catches what it raises instead of dying with it.

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


async def main() -> None:
    async with satay.run_app() as store:
        print("map:        ", await satay.start(fanout, [1, 2, 3], store=store).result())
        print("gather:     ", await satay.start(combined, 5, store=store).result())
        print("start_child:", await satay.start(parent, 4, store=store).result())

        # Parks on its two-second timer; the poll loop wakes it and result() waits.
        print("sleep:      ", await satay.start(paced, 10, store=store).result())

        pending = satay.start(review, 0, store=store)
        await satay.send_event(Approval(approved=True), key="order-42", store=store)
        print("event:      ", await pending.result())


asyncio.run(main())
```

Run it:

```console
$ python primitives.py
map:         [2, 4, 6]
gather:      [10, [2, 4]]
start_child: 9
sleep:       21
event:       approved
```

Nothing in that `main()` knows which of those workflows park and which do not, which is the point:
inside `run_app` a durable sleep is just a slow `await`.

## Recap

- `satay.sleep(duration)` parks the run on a durable timer. The process can exit and come back.
- `wait_for_event(Type, key=..., timeout=...)` parks until a matching event lands, resolving to
  `None` on timeout. `await satay.send_event(...)` delivers one from outside, and early delivery
  is buffered rather than lost.
- Parked runs need a poll loop: `async with satay.run_app() as store:` in your own script, or
  `satay dev --app`. Inside one, `result()` waits for the wake; outside one it returns
  `satay.PARKED`.
- `satay.map(task, items, key=...)` keys each item so a crash mid-fan-out resumes only the
  unfinished ones. `satay.gather(...)` rejoins mixed calls positionally.
- Both are fail-fast by default; `return_exceptions=True` collects instead, returning each slot's
  result or its `satay.TaskFailedError` and recording the failure in the journal.
- `satay.start_child` gives the child its own run id and journal, so it resumes rather than
  restarts.

## Next

[Testing Workflows](tutorial/testing.md). You have now written workflows that wait fourteen days
and retry with backoff, and neither is something you want a test suite to sit through.
