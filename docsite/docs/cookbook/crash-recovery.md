# Crash Recovery

A two-task workflow. The worker dies the instant the first task's result commits. A fresh
worker picks up the same run id, and the first task does not run again.

That absence is the product. This recipe makes it observable: a counter on disk records every
physical execution of each task body, so "it was reused" is a number you can read rather than
a claim the docs make.

Source: [`examples/crash_recovery_demo.py`](https://github.com/leejianrong/satay-runtime/blob/v0.1.0/examples/crash_recovery_demo.py)

## Get It And Run It

1. Install the runtime:

    ```bash
    pip install 'satay[studio]'
    ```

2. Fetch the file:

    ```bash
    curl -fsSL -O https://raw.githubusercontent.com/leejianrong/satay-runtime/v0.1.0/examples/crash_recovery_demo.py
    ```

3. Run it, naming a data directory so the journal outlives the process:

    ```bash
    SATAY_DATA_DIR=.satay-demo python crash_recovery_demo.py
    ```

## What It Printed

```console
$ SATAY_DATA_DIR=.satay-demo python crash_recovery_demo.py
phase 1: starting run 8980cbf25a004a7bbb62f68bc4f06fc5
phase 1: worker crashed — simulated crash after event 'TaskCompleted'
phase 1: step_one executions so far = 1
phase 1: step_two executions so far = 0

phase 2: resuming run 8980cbf25a004a7bbb62f68bc4f06fc5
phase 2: final result = 4 (expected 4)
phase 2: step_one executions = 1 (REUSED, still 1)
phase 2: step_two executions = 1 (ran once)

Run 8980cbf25a004a7bbb62f68bc4f06fc5 — 9 event(s)
    1  2026-07-31T07:40:17.136055+00:00  WorkflowCreated  workflow=demo code_version=git:4d22d57c0a914532d987bc7df2af0f65530cdce6
    2  2026-07-31T07:40:17.141316+00:00  TaskScheduled  task=step_one ordinal=0
    3  2026-07-31T07:40:17.145995+00:00  TaskAttemptStarted  task=step_one ordinal=0 attempt=1
    4  2026-07-31T07:40:17.150467+00:00  TaskCompleted  task=step_one ordinal=0
⚡   5  2026-07-31T07:40:17.162156+00:00  WorkflowResumed
    6  2026-07-31T07:40:17.171271+00:00  TaskScheduled  task=step_two ordinal=0
    7  2026-07-31T07:40:17.175743+00:00  TaskAttemptStarted  task=step_two ordinal=0 attempt=1
    8  2026-07-31T07:40:17.180621+00:00  TaskCompleted  task=step_two ordinal=0
    9  2026-07-31T07:40:17.184885+00:00  WorkflowCompleted

journal kept in …/.satay-demo
open it in Satay Studio:  satay dev --data-dir …/.satay-demo
or as text:               satay runs show 8980cbf25a004a7bbb62f68bc4f06fc5 --data-dir …/.satay-demo
```

The line to stare at is `step_one executions = 1 (REUSED, still 1)`. Phase 1 ran `step_one`
once. Phase 2 re-executed the workflow body from its first line, reached `await step_one(...)`
again, and got the recorded result back without calling your function. The counter never moved.

## The Workflow

The workflow itself is three lines, and it lives in the package as `satay.demo` so the
release-verification step can run it against a clean install:

```python
@task()
async def step_one(value: int) -> int:
    record_execution("step_one")
    return value + 1


@task()
async def step_two(value: int) -> int:
    record_execution("step_two")
    return value * 2


@workflow
async def demo(value: int) -> int:
    first = await step_one(value)
    second = await step_two(first)
    return second
```

`demo(1)` is `(1 + 1) * 2`, which is where the expected `4` comes from. `record_execution` is
the marker: it bumps a JSON file named by `SATAY_DEMO_MARKER`, so the count survives the crash
and phase 2 can read what phase 1 did.

## The Two Phases

```python
# -- Phase 1: worker runs, then dies right after step_one's TaskCompleted. --------
store = SQLiteStore.open(database)
injector = FaultInjector()
injector.crash_after("TaskCompleted")
handle = start(demo.demo, 1, store=store, injector=injector)
run_id = handle.run_id
try:
    await handle.result()
except SimulatedCrash as exc:
    print(f"phase 1: worker crashed — {exc}")
store.close()

# -- Phase 2: a fresh worker opens the same DB and resumes the same run. -----------
store = SQLiteStore.open(database)
resumed = start(demo.demo, 1, run_id=run_id, store=store)
result = await resumed.result()
```

Two details carry the whole recipe.

`injector.crash_after("TaskCompleted")` raises immediately after that event commits to SQLite.
The crash therefore lands at the hardest possible moment: durable state was written, and then
the process vanished. Nothing is mocked and nothing is rolled back. The journal is left exactly
as truncated as `kill -9` would leave it.

`run_id=run_id` on the second `start` is what makes phase 2 a **resume** rather than a second
run. Leave it off and you get a fresh run id and a full re-execution. It is the same handle you
would paste into a shell after a real crash, which is what the [quickstart](../quickstart.md)
has you do with a live `Ctrl-C`.

!!! info "Why both phases live in one process"

    In a real deployment phase 1 is a worker that dies and phase 2 is a fresh process opening
    the same `./.satay`. The example puts both in one program so it can run unattended in CI.
    The proof is unaffected: each phase does its own `SQLiteStore.open`, and phase 2 starts
    knowing nothing but the run id.

## Reading The Timeline

The last thing the file prints is the timeline, rendered the way `satay runs show` renders it.

`ordinal=0` on both tasks is not a bug. An ordinal counts calls **per task name**, so the first
call to `step_one` and the first call to `step_two` are both ordinal 0. That pair,
`(task_name, ordinal)`, is the durable-call identity the engine matches on during replay.

The `⚡` at sequence 5 marks `WorkflowResumed`. It says this run came back from an interruption
rather than waking gracefully from a timer, and it is the marker to look for when you want to
know whether a run was ever interrupted. A run that parks on a `satay.sleep` and wakes normally
gets no `⚡`, which the [timers recipe](timers-events.md) demonstrates.

After the marker, `step_two` is scheduled, attempted, and completed, and the run finishes with
the right answer. `step_one` never appears again.

## Open It In Studio

You passed `SATAY_DATA_DIR`, so the journal is still there. Boot the dev stack on it:

```bash
satay dev --data-dir .satay-demo
```

```console
$ satay dev --data-dir .satay-demo
app modules: none (no --app, no [tool.satay] app in pyproject.toml)
registered: 0 workflows; 0 tasks
  warning: 0 workflows registered — this process can serve Studio and read the journal, but
  it cannot start a run or wake one parked on a timer or event. Pass --app your.module to
  import your workflows.
policies: effect_safety=warn, nondeterminism=strict, version_mismatch=warn
INFO:     Started server process [768579]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:8787 (Press CTRL+C to quit)
Satay Studio:  http://127.0.0.1:8787/?token=THE_TOKEN_SATAY_DEV_PRINTED
  control/read API on http://127.0.0.1:8787  (session token required)
  press Ctrl-C to stop
```

The `0 workflows registered` warning is correct and harmless here. This run has already
finished, so there is nothing to start and nothing to wake. `--app` matters when you want the
dev stack to run your code, which the [Studio tour](studio-tour.md) covers.

!!! danger "Open the printed URL, `?token=` and all"

    Visiting `http://127.0.0.1:8787/` on its own serves the Studio page, and then every API
    call it makes comes back `401`, so you get an app shell that renders nothing. The token is
    minted fresh on every `satay dev`, so copy it out of the boot output each time. The
    [Studio page](../studio.md) has the rest, including the localStorage trick that saves you
    re-copying it on every restart.

In the run list, open the one completed `demo` run. The timeline shows the same nine events with
the interruption marked. Expand `TaskCompleted` for `step_one` and you can see the recorded
output value, which is literally what phase 2 read instead of calling your function.

Prefer text? Same journal, no browser:

```bash
satay runs show 8980cbf25a004a7bbb62f68bc4f06fc5 --data-dir .satay-demo
```

## Recap

- A crash after `TaskCompleted` commits is the hard case, and it is the one that works.
- Passing `run_id=` for an unfinished run resumes it. That argument is the whole difference
  between resuming and starting over.
- The workflow body re-executes from line one on every resume. Recorded calls answer from the
  journal; unrecorded ones execute for real.
- `⚡` on `WorkflowResumed` distinguishes an interruption from a graceful wake.
- Because the body re-runs, it has to be [deterministic](../determinism.md). That is the one
  rule with teeth.

Next: [Retries And Backoff](retries.md), where a task fails twice on purpose and the retry
schedule is readable off the journal.
