# Testing Workflows

A durable runtime is only worth having if you can prove it recovers. This page gets you a test
suite that crashes a workflow after a specific journal event, resumes it, and asserts the finished
task did not run twice. Plus a fourteen-day timer that fires in microseconds and a retry schedule
with no waiting in it.

The tools are in `satay.testing`, and they are part of the runtime rather than a test-only add-on.
The executor and the timer loop always take a clock, an RNG, and a fault hook. In production they
get the real ones. In a test you pass different ones.

| Tool | Replaces | So you can |
| --- | --- | --- |
| `ManualClock` | the wall clock | advance time by hand, and never wait |
| `SeededRng` | system entropy | get the same backoff jitter every run |
| `FaultInjector` | nothing (it adds a hook) | crash or stall right after a named journal event |

## Install the Fixtures

`satay.testing.fixtures` is a pytest plugin. Load it from your `conftest.py`:

```python title="conftest.py"
pytest_plugins = ["satay.testing.fixtures"]
```

Workflows and tasks are `async def`, so pytest needs to be able to run coroutines. This page uses
`pytest-asyncio` in auto mode, which is what the runtime's own suite uses:

```ini title="pytest.ini"
[pytest]
asyncio_mode = auto
```

```bash
pip install pytest pytest-asyncio
```

## The Code Under Test

Put this in `checkout.py`. It is the workflow from [First Steps](../quickstart.md) plus a
module-level counter, a variant that sleeps for two weeks, and a task that fails twice before
succeeding.

```python title="checkout.py"
from datetime import timedelta

import satay

#: Bumped every time a task body actually runs, so a test can prove reuse.
EXECUTIONS: dict[str, int] = {}


def ran(name: str) -> None:
    EXECUTIONS[name] = EXECUTIONS.get(name, 0) + 1


@satay.task()
async def charge(cents: int) -> str:
    ran("charge")
    return f"receipt-{cents}"


@satay.task()
async def email_receipt(receipt: str) -> str:
    ran("email_receipt")
    return f"emailed {receipt}"


@satay.workflow
async def checkout(cents: int) -> str:
    receipt = await charge(cents)
    return await email_receipt(receipt)


@satay.workflow
async def trial(cents: int) -> str:
    receipt = await charge(cents)
    await satay.sleep(timedelta(days=14))
    return await email_receipt(receipt)


@satay.task(retries=2)
async def settle(cents: int) -> str:
    ran("settle")
    if EXECUTIONS["settle"] < 3:
        raise RuntimeError("the bank hung up")
    return f"settled-{cents}"


@satay.workflow
async def settlement(cents: int) -> str:
    return await settle(cents)
```

That counter is the whole trick behind every assertion below. It counts **executions of the task
body**, so it distinguishes "the result came back" from "the work happened again".

Start `test_checkout.py` with the imports and one fixture that clears the counter:

```python title="test_checkout.py"
import asyncio

import pytest

import satay
from checkout import EXECUTIONS, charge, checkout, settlement, trial
from satay.journal.events import EventType
from satay.journal.store import SQLiteStore
from satay.testing import FaultInjector, ManualClock, SeededRng, SimulatedCrash
from satay.timers import TimerEventWorker


@pytest.fixture(autouse=True)
def reset_counters() -> None:
    EXECUTIONS.clear()
```

!!! note "Two of those imports sit below the public surface"

    `satay.journal.store.SQLiteStore`, `satay.journal.events.EventType`, and
    `satay.timers.TimerEventWorker` are not re-exported from `satay`, so they are more likely to
    move than `@task` or `satay.map`. Everything from `satay.testing` is a stable seam by design
    (ADR-0011). [Limits](../limits.md#maturity) lists the deeper imports these docs use.

## Test a Task on Its Own

A `@satay.task()` called outside a workflow is an ordinary coroutine. No journal, no store, no
replay. So the cheapest test you can write is the obvious one:

```python
async def test_charge_on_its_own() -> None:
    assert await charge(1999) == "receipt-1999"
    assert EXECUTIONS["charge"] == 1
```

Most of your logic belongs in tasks, which means most of your tests look like this. Reach for the
machinery below only when what you are testing is the orchestration.

## Test a Workflow Against an In-Memory Journal

`satay.start` takes a `store=`. Pass it an in-memory SQLite store and the run leaves nothing on
disk:

```python
async def test_checkout_completes() -> None:
    store = SQLiteStore.open(":memory:")
    handle = satay.start(checkout, 1999, store=store)

    assert await handle.result() == "emailed receipt-1999"
    assert await handle.status() == "completed"
    assert EXECUTIONS == {"charge": 1, "email_receipt": 1}
    store.close()
```

`SQLiteStore.open(":memory:")` creates and migrates the schema on connect, so there is no setup
step. If you want a real file (to inspect it after a failure, say), the plugin gives you
`temp_db_path`, a path under pytest's `tmp_path`.

## Crash It on Purpose

This is the test that matters. `FaultInjector.crash_after("TaskCompleted")` arms a fault: the next
time the journal commits an event of that type, the injector raises `SimulatedCrash` instead of
letting the drive continue.

```python hl_lines="4"
async def test_crash_after_charge_does_not_charge_twice() -> None:
    store = SQLiteStore.open(":memory:")
    injector = FaultInjector()
    injector.crash_after("TaskCompleted")

    handle = satay.start(checkout, 1999, store=store, injector=injector)
    with pytest.raises(SimulatedCrash):
        await handle.result()

    assert EXECUTIONS == {"charge": 1}

    resumed = satay.start(checkout, 1999, run_id=handle.run_id, store=store)
    assert await resumed.result() == "emailed receipt-1999"

    assert EXECUTIONS == {"charge": 1, "email_receipt": 1}

    events = await store.read_events(handle.run_id)
    charged = [
        e
        for e in events
        if e.type is EventType.TASK_COMPLETED and e.payload["task_name"] == "charge"
    ]
    assert len(charged) == 1
    assert any(e.type is EventType.WORKFLOW_RESUMED for e in events)
    store.close()
```

Read it as a story in four beats.

1. `charge` commits its `TaskCompleted`, and the fault fires. The process would be dead here; in
   the test the exception stands in for that.
2. `EXECUTIONS == {"charge": 1}` proves `charge` ran once and `email_receipt` never started.
3. `satay.start(..., run_id=handle.run_id, ...)` is the restart. Same run id, same store, no
   injector armed.
4. `charge` is **still** at 1 after the resume. It was answered from the journal. `email_receipt`
   is now at 1, so it ran for real.

The two journal assertions close the loop: exactly one `TaskCompleted` for `charge` (not two), and
a `WorkflowResumed`, which is the event Studio marks with a `⚡`.

!!! tip "The fault is armed on an event *type*, not a task name"

    `crash_after("TaskCompleted")` fires on the first `TaskCompleted` of the run, whichever task
    it belongs to. Pass `times=` to let it fire more than once. To crash before any body runs,
    arm `"TaskScheduled"` instead and watch the resumed run re-execute the task, because nothing
    was recorded.

    `stall_after(event_type)` is the other mode. It returns an `asyncio.Event` and blocks the
    commit until you set it, which is how you test what a reader sees while the single writer is
    mid-write.

## Skip a Fourteen-Day Sleep

`trial` parks on `satay.sleep(timedelta(days=14))`. A test cannot wait for that, and it does not
have to. Pass a `ManualClock` and the run's notion of time is entirely yours:

```python
async def test_a_fourteen_day_sleep_takes_no_time() -> None:
    clock = ManualClock()
    store = SQLiteStore.open(":memory:")
    handle = satay.start(trial, 1999, store=store, clock=clock)

    assert await handle.result() is None
    assert await handle.status() == "waiting"
    assert EXECUTIONS == {"charge": 1}

    worker = TimerEventWorker(store=store, clock=clock)
    assert await worker.tick() == 0

    clock.advance(14 * 24 * 3600)
    assert await worker.tick() == 1

    assert await handle.status() == "completed"
    assert await handle.result() == "emailed receipt-1999"
    assert EXECUTIONS == {"charge": 1, "email_receipt": 1}
    store.close()
```

Four things are being asserted here, and each one is a behaviour you would otherwise have to take
on faith.

`await handle.result()` returns `None` and the status is `waiting`. The run gave up its coroutine.
There is no frame to resume, only a timer row.

`await worker.tick() == 0` before advancing the clock. `tick()` returns how many timers it fired,
so zero is the proof that nothing was due yet.

`clock.advance(...)` then `tick() == 1`. One timer came due, the worker fired it, and the workflow
was re-driven to completion inside that call.

`charge` is still at 1. The wake replayed past it.

!!! info "Waking is not resuming"

    A timer wake is graceful, so it writes no `WorkflowResumed` and shows no `⚡`. That marker is
    reserved for a run that came back from an interruption.

## Pin the Backoff Jitter

`settle` has `retries=2` and fails twice, so a real run of it sleeps for a jittered backoff delay
between attempts. `SeededRng` makes that delay reproducible, and `ManualClock` makes it free.

Backoff waits happen inside the drive, so the test needs to advance virtual time while the drive is
suspended. That takes a small helper:

```python
async def drain(factory, clock, *, step=61.0, max_steps=500):
    """Drive a coroutine, advancing virtual time whenever it parks on the clock."""
    task = asyncio.ensure_future(factory())
    try:
        for _ in range(max_steps):
            for _ in range(4):
                await asyncio.sleep(0)
            if task.done():
                return await task
            if clock.pending_sleepers:
                clock.advance(step)
    finally:
        if not task.done():
            task.cancel()
    raise AssertionError("the run never settled")
```

`clock.pending_sleepers` is the count of coroutines currently suspended in `clock.sleep`. When it
is non-zero the drive is waiting on time and nothing else, so advancing is safe. The `step=61.0`
clears the 60-second backoff ceiling in one go.

```python
async def test_backoff_is_reproducible_under_a_seed() -> None:
    async def delays(seed: int) -> list[float]:
        EXECUTIONS.clear()
        clock = ManualClock()
        store = SQLiteStore.open(":memory:")
        handle = satay.start(settlement, 500, store=store, clock=clock, rng=SeededRng(seed))
        assert await drain(handle.result, clock) == "settled-500"
        assert EXECUTIONS["settle"] == 3
        events = await store.read_events(handle.run_id)
        store.close()
        return [
            e.payload["next_delay"]
            for e in events
            if e.type is EventType.TASK_ATTEMPT_FAILED
        ]

    assert await delays(1234) == await delays(1234)
    assert await delays(1234) != await delays(4321)
```

Three physical attempts for one logical call, and the two `TaskAttemptFailed` events carry the
delay the executor chose. Same seed, same delays. Different seed, different delays. That is what
lets you assert on a retry schedule at all.

## Run It

```console
$ pytest -v
============================= test session starts ==============================
platform linux -- Python 3.13.9, pytest-9.1.1, pluggy-1.6.0
rootdir: /home/you/checkout-demo
configfile: pytest.ini
plugins: asyncio-1.4.0
asyncio: mode=Mode.AUTO
collected 5 items

test_checkout.py::test_charge_on_its_own PASSED                          [ 20%]
test_checkout.py::test_checkout_completes PASSED                         [ 40%]
test_checkout.py::test_crash_after_charge_does_not_charge_twice PASSED   [ 60%]
test_checkout.py::test_a_fourteen_day_sleep_takes_no_time PASSED         [ 80%]
test_checkout.py::test_backoff_is_reproducible_under_a_seed PASSED       [100%]

============================== 5 passed in 0.11s ===============================
```

`0.11s`, containing a crash and a recovery, a fourteen-day timer, and a full three-attempt retry
schedule. No `sleep` anywhere in the suite.

## Assert on Outcomes, Not Internals

The seam is deliberately the public API driving real workflows against a real store. Tests that go
around it break on refactors that changed nothing a user can see. So assert on these:

- **The result.** `await handle.result()`.
- **The status.** `await handle.status()`, one of `running`, `waiting`, `completed`, `failed`,
  `cancelled`.
- **The exception.** `pytest.raises(satay.WorkflowFailedError)`, then check `error_type` and
  `error_message`.
- **The journal.** `await store.read_events(run_id)` gives you the event list. Assert on types,
  counts, and payload fields.
- **An execution counter** in your own code, like `EXECUTIONS` above. Nothing else distinguishes
  a reused result from a re-executed body.

And do not assert on the replay engine's internal state, on the identity resolver, or on how many
times an internal method was called. Those are not promises.

## The Fixtures

Everything the plugin provides:

| Fixture | Gives you |
| --- | --- |
| `manual_clock` | a fresh `ManualClock`, starting at `2026-01-01T00:00:00Z` |
| `seeded_rng` | a `SeededRng(1234)`, so jitter is the same on every run |
| `fault_injector` | a `FaultInjector`, cleared on teardown |
| `temp_data_dir` | a `.satay`-shaped directory under pytest's `tmp_path`, `blobs/` included |
| `temp_db_path` | the `satay.db` path inside it, for a real on-disk journal |
| `memory_db_path` | the string `":memory:"` |

The tests above construct their own objects so each one reads as a complete example. Using the
fixtures is shorter:

```python
async def test_with_fixtures(manual_clock, fault_injector, temp_db_path) -> None:
    store = SQLiteStore.open(temp_db_path)
    fault_injector.crash_after("TaskCompleted")
    ...
```

## Recap

- `satay.testing` ships `ManualClock`, `SeededRng`, and `FaultInjector`, plus pytest fixtures for
  all three and for temp store paths. Load them with
  `pytest_plugins = ["satay.testing.fixtures"]`.
- Tasks are ordinary coroutines outside a workflow, so test them directly.
- Pass `store=SQLiteStore.open(":memory:")` to `satay.start` to keep a run off disk.
- `injector.crash_after("TaskCompleted")` plus a second `satay.start` with the same `run_id` is
  the crash-recovery test. A module-level counter is what proves the finished task was reused.
- `ManualClock` plus `TimerEventWorker.tick()` fires a fourteen-day timer immediately, and
  `tick()` returns how many fired.
- `SeededRng` pins backoff jitter, and a small drain loop advances virtual time while the drive is
  suspended.
- Assert on results, statuses, exceptions, journal events, and your own counters. Nothing deeper.

## Next

That is the tutorial. The [Cookbook](../cookbook/index.md) has complete programs for the shapes
you will actually build: crash recovery, retries, timers and events, fan-out, an ELT pipeline, and
an agentic DAG with a human approval gate.
