# Timers And Events

Three runs in one file. One sleeps for eight hours. One blocks until somebody sends it an
event. One waits for an event that never arrives and takes its escalation branch instead.

The thing to watch is what the sleeping workflow costs while it waits, which is nothing. It does
not hold a coroutine open for eight hours. It records a timer, **parks**, and is released from
memory. A worker re-drives it when the deadline comes due.

Source: [`examples/timers_events_demo.py`](https://github.com/leejianrong/satay-runtime/blob/main/examples/timers_events_demo.py)

## Get It And Run It

```bash
pip install 'satay[studio] @ git+https://github.com/leejianrong/satay-runtime'
curl -fsSL -O https://raw.githubusercontent.com/leejianrong/satay-runtime/main/examples/timers_events_demo.py
SATAY_DATA_DIR=.satay-demo python timers_events_demo.py
```

## The Three Workflows

```python
@satay.workflow
async def overnight_restock(sku: str) -> str:
    """Count stock, wait out the 8-hour settlement window, then reorder."""
    on_hand = await count_stock(sku)
    await satay.sleep(8 * _HOUR)  # durable: recorded as a timer, the run parks here
    return await reorder(sku, on_hand)


@satay.workflow
async def await_shipment(order_id: str) -> str:
    """Block until a ``ShipmentArrived`` event lands on ``order_id``."""
    arrival = await satay.wait_for_event(ShipmentArrived, key=order_id)
    return f"{arrival.crates} crates received via {arrival.carrier}"


@satay.workflow
async def await_shipment_or_escalate(order_id: str) -> str:
    """The same wait, bounded. A timed-out wait resolves to ``None`` — not an error."""
    arrival = await satay.wait_for_event(ShipmentArrived, key=order_id, timeout=6 * _HOUR)
    if arrival is None:
        return f"escalated: nothing arrived for {order_id} within 6h"
    return f"{arrival.crates} crates received via {arrival.carrier}"
```

The event is a plain frozen dataclass:

```python
@dataclass(frozen=True)
class ShipmentArrived:
    carrier: str
    crates: int
```

`wait_for_event(ShipmentArrived, ...)` uses that class to rehydrate the delivered payload, so
what comes back on the other side of the wait is a `ShipmentArrived`, not a dict. The codec tags
and round-trips dataclasses, enums, datetimes, and timedeltas. There is no pickle involved
anywhere.

## What It Printed

```console
$ SATAY_DATA_DIR=.satay-demo python timers_events_demo.py
Satay — durable sleep, events, and the timeout path
data dir: …/.satay-demo

1) overnight_restock sleeps 8h — run dfda89e0f6ea4ce29ba925360cb88c1c
  first drive returned None — status waiting (parked)
  count_stock executions: 1  reorder: not yet reached
  worker tick with nothing due: 0 run(s) woken
  after advancing 8h, worker tick: 1 run(s) woken
  result: ordered 28 units of SKU-42   status: completed
  count_stock executions: 1 (reused on the wake, not re-run)  reorder: 1
  note: a graceful wake from a park is not an interruption — no ⚡ below

Run dfda89e0f6ea4ce29ba925360cb88c1c — 11 event(s)
    1  2026-01-01T00:00:00+00:00  WorkflowCreated  workflow=overnight_restock code_version=git:4d22d57c0a914532d987bc7df2af0f65530cdce6
    2  2026-01-01T00:00:00+00:00  TaskScheduled  task=count_stock ordinal=0
    3  2026-01-01T00:00:00+00:00  TaskAttemptStarted  task=count_stock ordinal=0 attempt=1
    4  2026-01-01T00:00:00+00:00  TaskCompleted  task=count_stock ordinal=0
    5  2026-01-01T00:00:00+00:00  TimerCreated
    6  2026-01-01T00:00:00+00:00  WorkflowWaiting
    7  2026-01-01T08:00:00+00:00  TimerFired
    8  2026-01-01T08:00:00+00:00  TaskScheduled  task=reorder ordinal=0
    9  2026-01-01T08:00:00+00:00  TaskAttemptStarted  task=reorder ordinal=0 attempt=1
   10  2026-01-01T08:00:00+00:00  TaskCompleted  task=reorder ordinal=0
   11  2026-01-01T08:00:00+00:00  WorkflowCompleted

2) await_shipment blocks on an event — run 2ea2c3cb195a4d36aaf4a99a9c4f97b8
  first drive returned None — status waiting
  sent ShipmentArrived(key='order-7') — it sits in the durable inbox
  worker tick: 1 run(s) woken
  result: 4 crates received via DHL   status: completed
  (an event that arrives *before* the wait is matched from the inbox too)

Run 2ea2c3cb195a4d36aaf4a99a9c4f97b8 — 5 event(s)
    1  2026-01-01T08:00:00+00:00  WorkflowCreated  workflow=await_shipment code_version=git:4d22d57c0a914532d987bc7df2af0f65530cdce6
    2  2026-01-01T08:00:00+00:00  EventWaitStarted
    3  2026-01-01T08:00:00+00:00  WorkflowWaiting
    4  2026-01-01T08:00:00+00:00  ExternalEventReceived
    5  2026-01-01T08:00:00+00:00  WorkflowCompleted

3) await_shipment_or_escalate times out — run 10b6f4ba03f94e9b8d28435ae6ea235f
  first drive returned None — status waiting
  after advancing 6h, worker tick: 1 run(s) woken
  result: escalated: nothing arrived for order-8 within 6h   status: completed
  the wait resolved to None and the workflow chose its own escalation branch

Run 10b6f4ba03f94e9b8d28435ae6ea235f — 6 event(s)
    1  2026-01-01T08:00:00+00:00  WorkflowCreated  workflow=await_shipment_or_escalate code_version=git:4d22d57c0a914532d987bc7df2af0f65530cdce6
    2  2026-01-01T08:00:00+00:00  TimerCreated
    3  2026-01-01T08:00:00+00:00  EventWaitStarted
    4  2026-01-01T08:00:00+00:00  WorkflowWaiting
    5  2026-01-01T14:00:00+00:00  TimerFired
    6  2026-01-01T14:00:00+00:00  WorkflowCompleted

journal kept in …/.satay-demo
all three runs are on the run list:  satay dev --data-dir …/.satay-demo
```

## Parking Is The Whole Feature

Section 1's first line is the surprising one:

```
first drive returned None — status waiting (parked)
```

`await handle.result()` returned `None` and the run is not finished. That is not an error. The
workflow reached `satay.sleep(8 * _HOUR)`, the runtime wrote `TimerCreated` and
`WorkflowWaiting`, and the drive **returned**. The coroutine is gone. Nothing about this run
lives in memory any more; all of it is three rows in SQLite.

Then when the deadline comes due, a worker re-drives the run from the top. `count_stock` answers
from the journal, `satay.sleep` sees a fired timer and returns immediately, and `reorder` runs
for real. `count_stock executions: 1` on the far side of the wake is the proof.

This is why `satay.sleep` exists as a primitive rather than being `asyncio.sleep`. An
`asyncio.sleep(8 * 3600)` in a workflow body pins a coroutine to one process for eight hours and
loses the workflow entirely if that process restarts. A durable sleep survives a restart because
there is nothing to lose.

!!! warning "No `⚡` here, and that is deliberate"

    A graceful wake from a park appends no `WorkflowResumed`, so nothing on this timeline is
    marked as an interruption. `⚡` means the process died. If parking looked like crashing you
    would have no way to tell a healthy overnight wait from a worker that keeps falling over.

## Events Land In A Durable Inbox

Section 2 sends the event while the run is parked:

```python
shipment = satay.start(await_shipment, "order-7", store=store, clock=clock)
await satay.send_event(ShipmentArrived(carrier="DHL", crates=4), key="order-7", store=store)
print(f"  worker tick: {await worker.tick()} run(s) woken")
```

`send_event` does not deliver into a coroutine. It writes to a persistent inbox keyed by
`key="order-7"`. Delivery happens on the next worker tick, which finds a parked run whose
outstanding wait matches an inbox entry and re-drives it.

Because the inbox is durable, ordering does not have to be lucky. An event that arrives *before*
the workflow reaches its `wait_for_event` is matched out of the inbox when the wait finally
happens. You do not have to sequence your senders against your workflow's progress.

`key=` is what routes it. Use the correlation id of the thing you are waiting on: an order id, a
review id, a ticket number. Buffered matches for the same key are consumed FIFO by arrival time.

## A Timeout Is A Value, Not An Exception

Section 3 is the branch most people get wrong on the first try:

```python
arrival = await satay.wait_for_event(ShipmentArrived, key=order_id, timeout=6 * _HOUR)
if arrival is None:
    return f"escalated: nothing arrived for {order_id} within 6h"
```

A timed-out wait resolves to `None`. It does not raise. The workflow decides what a timeout
means, and the run completes normally through its own escalation path. Notice the status in the
output: `completed`, not `failed`. Nothing went wrong; a business rule fired.

Its timeline shows both mechanisms armed at once. `TimerCreated` for the deadline,
`EventWaitStarted` for the wait, then `WorkflowWaiting`. Whichever resolves first ends the wait.

!!! info "A delivered event beats a simultaneously-due timeout"

    The worker's poll loop delivers events **before** it fires due timers. So when an event
    arrives in the same tick that the timeout comes due, the event wins and your workflow gets
    the payload rather than `None`. A timeout timer whose wait has already been resolved by an
    event is discarded rather than fired. That ordering is decided in the runtime, not left to
    chance.

## Where The Worker Comes From

The example drives the poll loop by hand, one tick at a time:

```python
clock = ManualClock()
worker = TimerEventWorker(store=store, clock=clock)
...
print(f"  worker tick with nothing due: {await worker.tick()} run(s) woken")
clock.advance(8 * 3600)  # 8 hours later, as far as the runtime is concerned
print(f"  after advancing 8h, worker tick: {await worker.tick()} run(s) woken")
```

`tick()` returns how many runs it woke, which is why the first tick prints `0` and the one after
the clock advance prints `1`. Nothing was due yet, then something was.

In production you do not write this loop. `satay dev` runs `TimerEventWorker` in the background
against a real clock, and it is the same class doing the same job. The only reason it appears here
is that a demo cannot wait eight real hours, and `ManualClock` plus an explicit `tick()` is how
you compress that to a microsecond.

Which is also how you should test this. `clock.advance(...)` then `await worker.tick()` is the
whole pattern for asserting on a workflow that sleeps overnight or waits three days for a human.
See [Testing workflows](../tutorial/testing.md).

## Open It In Studio

```bash
satay dev --data-dir .satay-demo
```

Open the printed URL with its `?token=` query string. All three runs are in the list, all
`completed`.

Two things are worth clicking for. On run 1, the gap between `WorkflowWaiting` and `TimerFired`
is eight hours of journal time with nothing in between, which is what "it costs nothing to wait"
looks like on a timeline. On run 3, `TimerCreated` and `EventWaitStarted` sit next to each other,
so you can see both mechanisms armed and watch which one resolved the wait.

!!! tip "`satay dev` can wake your parked runs too"

    These three runs have finished, so there is nothing left to wake. But if you park a run of
    your own and quit the script, `satay dev --app your.module` picks it up: the poll loop wakes
    any run in the journal whose timer is due, no matter which process parked it. The journal is
    the only state that matters. The [Studio tour](studio-tour.md) shows that end of it.

## Recap

- `satay.sleep` records a timer and parks the run. The coroutine is released; only SQLite rows
  remain. An eight-hour wait costs nothing to hold.
- A parked run's first drive returns `None` with status `waiting`. That is the normal shape, not
  a failure.
- Waking from a park is graceful. No `WorkflowResumed`, no `⚡`. That marker stays reserved for
  actual interruptions.
- `send_event` writes to a durable inbox keyed by `key=`. An event that arrives before its wait
  is still matched, so senders and workflows do not have to be sequenced.
- A timed-out `wait_for_event` returns `None` rather than raising, and the workflow chooses what
  to do about it.
- A delivered event beats a simultaneously-due timeout, because the poll loop delivers before it
  fires timers.

Next: [Fan-Out With Crash Recovery](fan-out.md), the demo that tends to convince people.
