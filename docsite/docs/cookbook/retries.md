# Retries And Backoff

A task that fails twice and succeeds on the third attempt, then a task that runs out of
retries and takes the run down with it.

The interesting part is not that retries happen. It is that every attempt is a journal event,
so the retry schedule is durable state you can read back, weeks later, from a run that failed
at 3am. It is not a log line you have to hope somebody kept.

Source: [`examples/retries_backoff_demo.py`](https://github.com/leejianrong/satay-runtime/blob/v0.1.0a3/examples/retries_backoff_demo.py)

## Get It And Run It

```bash
pip install 'satay[studio]'
curl -fsSL -O https://raw.githubusercontent.com/leejianrong/satay-runtime/v0.1.0a3/examples/retries_backoff_demo.py
SATAY_DATA_DIR=.satay-demo python retries_backoff_demo.py
```

## The Flaky Task

```python
@satay.task(retries=2)
async def fetch_rate(pair: str) -> float:
    """A flaky read: the upstream times out twice, then answers (three attempts)."""
    record("fetch_rate")
    ctx = satay.task_context()
    if ctx.attempt < SUCCEEDS_ON_ATTEMPT:
        raise RuntimeError(f"upstream rate API timed out (attempt {ctx.attempt})")
    return 1.35


@satay.task()
async def convert(amount: float, rate: float) -> float:
    """Apply the fetched rate. Runs once — the retries upstream are invisible here."""
    record("convert")
    return round(amount * rate, 2)


@satay.workflow
async def quote(amount: float) -> float:
    rate = await fetch_rate("USD/SGD")
    return await convert(amount, rate)
```

`retries=2` means two retries on top of the first attempt, so three physical attempts at most.
The failure decision reads `ctx.attempt` off the task context, which is why the schedule comes
out the same on every machine.

Notice what `quote` does not contain: no `try`, no attempt counter, no backoff loop. The
workflow body reads as if `fetch_rate` simply works. Retrying is the executor's job.

## What It Printed

```console
$ SATAY_DATA_DIR=.satay-demo python retries_backoff_demo.py
Satay — retries and capped exponential backoff
data dir: …/.satay-demo

1) fetch_rate fails twice then succeeds — run 360ca4293b34439f80e0c3bb8486cb9e
  attempt 1  FAILED   RuntimeError: upstream rate API timed out (attempt 1)  (backoff 0.966s)
  attempt 2  FAILED   RuntimeError: upstream rate API timed out (attempt 2)  (backoff 0.881s)
  attempt 3  SUCCEEDED
  result: 135.0 SGD   status: completed
  fetch_rate bodies executed: 3 (at-least-once, by design)
  convert bodies executed:    1 (the retries never reach it)
  backoff scheduled: 1.848s in total — none of it real time

Run 360ca4293b34439f80e0c3bb8486cb9e — 12 event(s)
    1  2026-01-01T00:00:00+00:00  WorkflowCreated  workflow=quote code_version=git:4d22d57c0a914532d987bc7df2af0f65530cdce6
    2  2026-01-01T00:00:00+00:00  TaskScheduled  task=fetch_rate ordinal=0
    3  2026-01-01T00:00:00+00:00  TaskAttemptStarted  task=fetch_rate ordinal=0 attempt=1
    4  2026-01-01T00:00:00+00:00  TaskAttemptFailed  task=fetch_rate ordinal=0 attempt=1 error=RuntimeError: upstream rate API timed out (attempt 1) next_delay=0.966s
    5  2026-01-01T00:01:01+00:00  TaskAttemptStarted  task=fetch_rate ordinal=0 attempt=2
    6  2026-01-01T00:01:01+00:00  TaskAttemptFailed  task=fetch_rate ordinal=0 attempt=2 error=RuntimeError: upstream rate API timed out (attempt 2) next_delay=0.881s
    7  2026-01-01T00:02:02+00:00  TaskAttemptStarted  task=fetch_rate ordinal=0 attempt=3
    8  2026-01-01T00:02:02+00:00  TaskCompleted  task=fetch_rate ordinal=0
    9  2026-01-01T00:02:02+00:00  TaskScheduled  task=convert ordinal=0
   10  2026-01-01T00:02:02+00:00  TaskAttemptStarted  task=convert ordinal=0 attempt=1
   11  2026-01-01T00:02:02+00:00  TaskCompleted  task=convert ordinal=0
   12  2026-01-01T00:02:02+00:00  WorkflowCompleted

2) fetch_from_dead_host exhausts retries=1 — run 0f8183810914424b9a2339943d038c59
  raised ConnectionError: no route to rates.invalid while fetching USD/SGD
  status: failed
  attempts made: 2 (retries=1 → 1 + 1 retry)
  the last error is what the run fails with; earlier attempts stay on the journal

  attempt 1  FAILED   ConnectionError: no route to rates.invalid while fetching USD/SGD  (backoff 0.966s)
  attempt 2  FAILED   ConnectionError: no route to rates.invalid while fetching USD/SGD  (no retry left)

journal kept in …/.satay-demo
browse both runs:  satay dev --data-dir …/.satay-demo
or as text:        satay runs show 360ca4293b34439f80e0c3bb8486cb9e --data-dir …/.satay-demo
```

## Reading The Retry Schedule

Three things are worth pulling out of that timeline.

**One `TaskScheduled`, three `TaskAttemptStarted`.** The task was scheduled once and attempted
three times, all at `ordinal=0`. One logical durable call, several physical attempts. That
distinction is the reason `convert` ran exactly once: it is downstream of the *logical* call, and
the logical call succeeded once.

**`next_delay` on each `TaskAttemptFailed`.** That is the computed backoff before the next
attempt, recorded on the journal. `0.966s`, then `0.881s`. The formula is
`base * 2 ** (failure - 1)` with **full jitter** and a 60-second cap, and the jitter is drawn
from the injected RNG, which is why the example pins `SeededRng(1234)` and gets the same two
numbers every run.

Full jitter is why the second delay is *smaller* than the first. The exponential term doubles the
**ceiling**, and the actual delay is a uniform draw below it. A run of delays that only ever
grows is not full jitter, it is a thundering herd waiting to happen.

**`convert bodies executed: 1`, `fetch_rate bodies executed: 3`.** At-least-once execution, out
loud. Satay guarantees your task body runs *at least* once per logical call, never that it runs
exactly once. Three attempts means the body ran three times, and if that body writes
somewhere, it wrote three times. See [Guarantees](../guarantees.md) for the idempotency contract
that goes with this, and the [ELT pipeline recipe](elt-pipeline.md) for the damage when you skip
it.

## Exhaustion Is A Different Story

The second run never succeeds:

```python
@satay.task(retries=1)
async def fetch_from_dead_host(pair: str) -> float:
    """A read that never succeeds: two attempts, then the run fails terminally."""
    record("fetch_from_dead_host")
    raise ConnectionError(f"no route to rates.invalid while fetching {pair}")
```

`retries=1` allows two attempts. After the second failure there is no retry left, the delay
column says `no retry left`, and the run goes to `failed` with the **last** error. Earlier
attempts are not overwritten. They stay on the journal, each with its own error and its own
recorded delay, so a post-mortem can see whether the first failure differed from the last.

A failed run is terminal. Calling `satay.start(..., run_id=...)` on it re-raises rather than
resuming. To get back in you fork it, which the [agentic DAG recipe](agentic-dag.md) does at the
end.

## Nobody Waited 1.8 Seconds

Look at the timestamps: the run jumps a minute between attempts, while the recorded delays are
under a second. Both are true, and the gap is the point.

```python
from satay.testing import ManualClock, SeededRng, settle

handle = satay.start(quote, 100.0, store=store, clock=clock, rng=SeededRng(JITTER_SEED))
result = await settle(handle.result, clock)
```

Backoff waits go through the **injected clock**. Pass `satay.testing.ManualClock` and nothing
moves until someone calls `clock.advance(...)`, so a retry schedule replays in zero wall-clock
time. Somebody still has to move that clock, and that somebody ships with Satay: `settle` drives
an awaitable and advances the clock through every wait the drive suspends on.

Its default `step` is a deliberately coarse 61 seconds. One advance clears the 60-second backoff
cap, which is why the journal timestamps move a minute at a time while the *recorded* delays stay
sub-second. Virtual time is free, so precision buys nothing.

Two behaviours worth knowing before you lean on it. A drive that **parks** — on a durable timer
or an event wait — returns `satay.PARKED` normally, because parking is a result rather than a
stall, and only the caller can produce the worker tick that would unpark it. A drive that never
finishes raises
`NeverSettledError` after `max_steps` passes, having cancelled the drive first, so a test that
accidentally waits on real time fails with a diagnosis instead of hanging your suite.

!!! tip "This is the loop your tests want"

    `settle` is not demo scaffolding — it is the same helper your own tests want, and the `drain`
    fixture in `satay.testing.fixtures` hands you this exact function. Test a retry policy this
    way and your suite asserts the real recorded delays without sleeping for them.
    [Testing workflows](../tutorial/testing.md) has the full pattern.

## The Guard On Side-Effecting Retries

`fetch_rate` is a read, which is why it declares no `side_effect=`. Its docstring flags the rule
that applies when a retryable task writes:

```python
"""
This task is a read, which is why it declares no ``side_effect=``. A *retryable*
side-effecting task has to promise ``idempotent=True`` (that it keys its effect on
``ctx.idempotency_key``) or ``effect_safety=strict`` rejects it at schedule time.
"""
```

`effect_safety` defaults to `warn`, so by default you get a log line rather than a refusal. Set
it to `strict` and a retryable `side_effect=True` task that has not promised `idempotent=True`
will not be scheduled at all. The [ELT pipeline recipe](elt-pipeline.md) runs both loaders side
by side and counts the duplicated rows.

## Open It In Studio

```bash
satay dev --data-dir .satay-demo
```

Open the printed URL with its `?token=` query string. Both runs are in the run list, one
`completed` and one `failed`.

Click into `quote`, then into the `fetch_rate` call in the execution tree. Task detail is the
view that earns its keep here: it stacks all three attempts in one place, each with its error
and its recorded backoff, instead of making you scan a flat log for the ones that share an
ordinal.

## Recap

- `retries=N` gives `N + 1` physical attempts, and the executor owns the loop. Your workflow
  body stays free of retry plumbing.
- One logical durable call, several physical attempts, all at the same ordinal.
- Every attempt and every computed `next_delay` lands on the journal, so the schedule is
  readable after the fact.
- Backoff is capped exponential with full jitter off the injected RNG. Delays can shrink; that
  is the jitter working.
- Exhausting retries fails the run with the last error. Earlier attempts survive on the journal,
  and the run is terminal.
- Three attempts means the body ran three times. At-least-once is a promise about the
  minimum, so a writing task needs [an idempotency key](../guarantees.md).

Next: [Timers And Events](timers-events.md), where a workflow sleeps for eight hours without
holding a coroutine open.
