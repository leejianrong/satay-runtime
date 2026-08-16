# A Studio Tour

The other recipes each show one primitive. This one builds a **single run** that touches nearly
all of them: a keyed fan-out with a retried item, a crash and resume, an eight-hour sleep, an
external event, a linked child workflow, and self-reported model usage. Plus a second run that
fails outright, so the run list has both outcomes to compare.

Then it boots the debugger and tells you what to click.

Source: [`examples/studio_walkthrough.py`](https://github.com/leejianrong/satay-runtime/blob/v0.1.0a3/examples/studio_walkthrough.py)

## Get It And Run It

This is the one recipe that needs the `studio` extra, because the payoff is the UI:

```bash
pip install 'satay[studio]'
curl -fsSL -O https://raw.githubusercontent.com/leejianrong/satay-runtime/v0.1.0a3/examples/studio_walkthrough.py
SATAY_DATA_DIR=.satay-demo python studio_walkthrough.py
```

Naming the data directory is not optional here. Without it the journal goes to a temp directory
your OS may clear at any moment, and there is nothing left to open. The file says so if you
forget.

## The Run It Builds

```python
@satay.workflow
async def morning_digest(topic: str) -> dict[str, object]:
    """Fan out, park overnight, wait for approval, then hand off to a child workflow."""
    feeds = await satay.map(fetch_feed, SOURCES, key=source_key)
    total = sum(int(feed["items"]) for feed in feeds)
    summary = await summarize(total)

    await satay.sleep(timedelta(hours=8))  # parks until the send window opens

    approval = await satay.wait_for_event(
        PublishApproval, key=APPROVAL_KEY, timeout=timedelta(hours=12)
    )
    if approval is None or not approval.approved:
        return {"topic": topic, "published": False, "reason": "no approval"}

    child = await satay.start_child(publish_digest, [summary, approval.reviewer])
    body: str = await child.result()
    return {"topic": topic, "published": True, "items": total, "bytes": len(body)}
```

One of the four feeds is flaky, so there is a retry on the timeline:

```python
@satay.task(retries=2)
async def fetch_feed(source: str) -> dict[str, object]:
    """Fetch one feed. ``arxiv`` times out on its first attempt, then answers."""
    record(f"fetch:{source}")
    ctx = satay.task_context()
    if source == FLAKY_SOURCE and ctx.attempt == 1:
        raise TimeoutError(f"{source} feed timed out on attempt {ctx.attempt}")
    return {"source": source, "items": len(source) * 3}
```

And one task self-reports what it spent, which is what puts a usage panel in the task detail:

```python
@satay.task()
async def summarize(items: int) -> str:
    """Summarise the batch and self-report model usage into the journal's usage slot."""
    record("summarize")
    satay.task_context().record_model_usage(
        model="demo-summarizer-v1", input_tokens=items * 40, output_tokens=120
    )
    return f"summary of {items} items"
```

## What It Printed

```console
$ SATAY_DATA_DIR=.satay-demo python studio_walkthrough.py
Satay — building a run worth looking at
data dir: …/.satay-demo

1. fan out over 4 feeds (arxiv needs a retry), then crash
   worker died: simulated crash after event 'TaskCompleted'
   durably recorded before the crash: ['source-hn']
   feeds actually fetched: {'fetch:hn': 1}
2. restart the same run — recorded feeds are reused, unresolved ones re-run
   drive returned <parked>; status waiting (parked on the 8h sleep)
     source-hn            fetched 1x — REUSED from the journal — never fetched twice
     source-lobsters      fetched 1x — had not started yet — fetched on the restart
     source-arxiv         fetched 2x — had not started yet — fetched on the restart
     source-changelog     fetched 1x — had not started yet — fetched on the restart
   (at-least-once: only a *committed* result is reused, which is exactly why a
    retryable side-effecting task has to declare idempotent=True)
3. eight hours later: advance the clock and let the worker fire the timer
   tick woke 1 run(s)
   status waiting (now parked on the approval event)
4. approve it: send_event, then one more tick delivers it
   tick woke 1 run(s)
   result: {'topic': 'durable execution', 'published': True, 'items': 72, 'bytes': 51}
   status: completed
5. and one run that fails outright, to compare against
   failed with PermissionError: premium-wire returned 402 Payment Required

the run: 28 events, 13 distinct types
  interruption (⚡) at seq: [5]
  recorded model usage: [{'model': 'demo-summarizer-v1', 'input_tokens': 2880, 'output_tokens': 120}]
  child run: 1f71c2bcfce44ada91f3f3ae99b5c1a4 (publish_digest)
  runs in the data dir: 3
```

Thirteen distinct event types in one run, which is the point. Here is the timeline it prints
next:

```console
Run 1e909aadfbd34ba2b43a0c6a3bd657d2 — 28 event(s)
    1  2026-01-01T00:00:00+00:00  WorkflowCreated  workflow=morning_digest code_version=src:21b941c439f0af5c
    2  2026-01-01T00:00:00+00:00  TaskScheduled  task=fetch_feed key=source-hn
    3  2026-01-01T00:00:00+00:00  TaskAttemptStarted  task=fetch_feed key=source-hn attempt=1
    4  2026-01-01T00:00:00+00:00  TaskCompleted  task=fetch_feed key=source-hn
⚡   5  2026-01-01T00:00:00+00:00  WorkflowResumed
    6  2026-01-01T00:00:00+00:00  TaskScheduled  task=fetch_feed key=source-lobsters
    7  2026-01-01T00:00:00+00:00  TaskAttemptStarted  task=fetch_feed key=source-lobsters attempt=1
    8  2026-01-01T00:00:00+00:00  TaskCompleted  task=fetch_feed key=source-lobsters
    9  2026-01-01T00:00:00+00:00  TaskScheduled  task=fetch_feed key=source-arxiv
   10  2026-01-01T00:00:00+00:00  TaskAttemptStarted  task=fetch_feed key=source-arxiv attempt=1
   11  2026-01-01T00:00:00+00:00  TaskAttemptFailed  task=fetch_feed key=source-arxiv attempt=1 error=TimeoutError: arxiv feed timed out on attempt 1 next_delay=0.736s
   12  2026-01-01T00:00:00+00:00  TaskScheduled  task=fetch_feed key=source-changelog
   13  2026-01-01T00:00:00+00:00  TaskAttemptStarted  task=fetch_feed key=source-changelog attempt=1
   14  2026-01-01T00:00:00+00:00  TaskCompleted  task=fetch_feed key=source-changelog
   15  2026-01-01T00:01:01+00:00  TaskAttemptStarted  task=fetch_feed key=source-arxiv attempt=2
   16  2026-01-01T00:01:01+00:00  TaskCompleted  task=fetch_feed key=source-arxiv
   17  2026-01-01T00:01:01+00:00  TaskScheduled  task=summarize ordinal=0
   18  2026-01-01T00:01:01+00:00  TaskAttemptStarted  task=summarize ordinal=0 attempt=1
   19  2026-01-01T00:01:01+00:00  TaskCompleted  task=summarize ordinal=0
   20  2026-01-01T00:01:01+00:00  TimerCreated
   21  2026-01-01T00:01:01+00:00  WorkflowWaiting
   22  2026-01-01T08:01:01+00:00  TimerFired
   23  2026-01-01T08:01:01+00:00  TimerCreated
   24  2026-01-01T08:01:01+00:00  EventWaitStarted
   25  2026-01-01T08:01:01+00:00  WorkflowWaiting
   26  2026-01-01T08:01:01+00:00  ExternalEventReceived
   27  2026-01-01T08:01:01+00:00  ChildWorkflowScheduled  child=publish_digest run_id=1f71c2bcfce44ada91f3f3ae99b5c1a4
   28  2026-01-01T08:01:01+00:00  WorkflowCompleted
```

Note sequence 11 and 15: the `source-arxiv` retry happens **around** the `source-changelog` fetch,
because the fan-out kept working while that item backed off. And note `code_version=src:...` rather
than `git:...`. Outside a git checkout the code-version stamp falls back to a hash of your source,
which is worth knowing before a version-mismatch chip surprises you in the UI.

## Boot The Dev Stack

```bash
satay dev --app studio_walkthrough --data-dir .satay-demo
```

```console
$ satay dev --app studio_walkthrough --data-dir .satay-demo
app modules (--app): studio_walkthrough
registered: 3 workflows (morning_digest, paywalled_digest, publish_digest); 4 tasks (fetch_feed, fetch_paywalled_feed, render_email, summarize)
policies: effect_safety=warn, nondeterminism=strict, version_mismatch=warn
INFO:     Started server process [797808]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:8787 (Press CTRL+C to quit)
Satay Studio:  http://127.0.0.1:8787/?token=THE_TOKEN_SATAY_DEV_PRINTED
  control/read API on http://127.0.0.1:8787  (session token required)
  press Ctrl-C to stop
```

One process is doing four jobs: an exclusive lock on the data directory, the SQLite store, the
timer and event poll loop, and the HTTP server that serves both the read API and the Studio
bundle. Startup order is lock, store, worker, server; shutdown is the exact reverse.

It binds `127.0.0.1:8787` by default. `--port 0` picks an ephemeral one.

### `--app` Is What Makes It More Than A Viewer

`registered: 3 workflows` is the line that matters. A workflow only exists as far as the runtime
is concerned once its decorator has run, which happens when Python imports the module. `--app`
tells the dev stack which modules to import.

Leave it off and you get this instead:

```console
app modules: none (no --app, no [tool.satay] app in pyproject.toml)
registered: 0 workflows; 0 tasks
  warning: 0 workflows registered — this process can serve Studio and read the journal, but
  it cannot start a run or wake one parked on a timer or event. Pass --app your.module to
  import your workflows.
```

That is a fine way to read a finished journal, and it is all the earlier recipes needed. It is not
a way to run anything. With `--app`, the same process can start your workflows over the API and
wake runs parked on a timer or an event, including runs some other process parked an hour ago and
then quit. The journal is the only state that matters.

`--app` takes a **dotted module path**, not a filename. Your working directory is added to the end
of `sys.path`, so the bare `studio_walkthrough` works for a file you downloaded into the current
directory. From a clone of the repository it would be `--app examples.studio_walkthrough`.

!!! warning "One `satay dev` per data directory"

    The first thing it does is take an exclusive advisory lock. A second one on the same directory
    is refused rather than allowed to race the single-writer journal:

    ```console
    $ satay dev --data-dir .satay-demo --port 8788
    error: another satay dev process holds the lock on .satay-demo/dev.lock (pid=797808);
    refusing to start a second writer on the same data directory, which would race the
    single-writer journal into corruption (ADR-0017/Q54). Stop that process, or run with
    a different --data-dir.
    ```

    The lock is POSIX `flock`, so on Windows it degrades to a no-op and this protection is not
    there.

### Open The URL Whole

!!! danger "The `?token=` is part of the URL"

    Open the printed URL including its query string. Visiting `http://127.0.0.1:8787/` on its own
    serves the page and then every API call it makes comes back `401`, so you get an app shell
    that renders nothing and an error in the console. A fresh token is minted on every boot.

Verified against the live stack above:

```console
$ TOKEN=the-token-satay-dev-printed

$ curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8787/
200                          # the static page is deliberately unauthenticated

$ curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8787/runs
401                          # the API is not

$ curl -s -o /dev/null -w '%{http_code}\n' \
    -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8787/runs
401                          # there is no bearer-token support

$ curl -s -o /dev/null -w '%{http_code}\n' \
    -H "X-Satay-Token: $TOKEN" http://127.0.0.1:8787/runs
200                          # this is the header
```

The page loads unauthenticated on purpose: the browser has to fetch the bundle before it has
anywhere to put a token, so the guard sits on the API rather than on the HTML. Which is exactly why
the symptom of a missing token is an empty app rather than a login screen.

And the header is `X-Satay-Token`. `Authorization: Bearer` gets the same `401` as sending nothing,
which makes it look like your token is wrong when the header name is what is wrong.

!!! tip "Piping `satay dev` to a file?"

    Run it with `PYTHONUNBUFFERED=1`. Its stdout is block-buffered when it is not a terminal, so
    the tokenized URL sits in the buffer and never appears in your log.

## The Tour

Open the run list. Three runs: `morning_digest` (completed), `publish_digest` (completed, the
child), and `paywalled_digest` (failed).

**Runs.** Workflow name, status, creation time, and a version-mismatch chip when the code has
moved since the run was stamped. You will see those chips often and often harmlessly: a `satay dev`
started from a different working directory computes a different source hash, so the chip lights up
even though nothing about your code changed. Read it as a prompt to check, not a verdict.

**Timeline.** Open `morning_digest`. The `⚡` at sequence 5 is where the worker died. Expand any
event for its payload. Every event also offers "fork before here".

**Execution tree.** The same run as a tree of durable calls rather than a flat log. This is where
the fan-out reads as a fan-out. Here it is over the API, which is the shape the UI renders:

```console
$ curl -s -H "X-Satay-Token: $TOKEN" \
    http://127.0.0.1:8787/runs/1e909aadfbd34ba2b43a0c6a3bd657d2/tree | python -m json.tool
{
    "run_id": "1e909aadfbd34ba2b43a0c6a3bd657d2",
    "workflow_name": "morning_digest",
    "status": "completed",
    "nodes": [
        {
            "kind": "map",
            "group": "map:0:fetch_feed",
            "task_name": "fetch_feed",
            "items": [
                {
                    "kind": "task",
                    "identity": "fetch_feed:key:source-arxiv",
                    "task_name": "fetch_feed",
                    "status": "completed",
                    "attempts": 2,
                    "key": "source-arxiv"
                },
```

Excerpted: three more items follow, then `summarize`, then the child run. Note
`"attempts": 2` on `source-arxiv` and `1` on its siblings, and `identity` keyed on
`key:source-arxiv` rather than on a position.

**Task detail.** Click `fetch_feed / source-arxiv`. One durable call in full: its identity, both
attempts with the `TimeoutError` and the `0.736s` backoff, and the recorded input and output. Then
click `summarize` for the usage panel, which is the `record_model_usage` call from earlier rendered
as `demo-summarizer-v1, 2880 in, 120 out`.

**The run tree across runs.** Sequence 27 is `ChildWorkflowScheduled`, and `publish_digest` is
linked both ways: the parent names the child run id, the child records its `parent_run_id`. Studio
nests it, so `start_child` reads as a second level rather than as an unrelated run that happens to
share a timestamp.

**The failed run.** Open `paywalled_digest`. Two attempts and a recorded traceback for a
`PermissionError`. Having it next to a completed run in the same list is the reason the example
bothers to create it.

### Fork And Compare

Forking is the one write Studio makes. It copies the source run's journal up to a chosen event into
a brand new run, then drives that new run forward under whatever your code says **now**. The source
run is untouched.

On any timeline event, "fork before here". Then follow the lineage chip on the new run's header to
**Compare**, which matches the two runs by durable-call identity rather than by sequence number, so
you can see which calls the fork replayed from the journal and which it re-ran.

The [agentic DAG recipe](agentic-dag.md) does this for real: it forks a finished run just before
its synthesis step, changes the prompt, and re-runs one model call instead of nine.

Over the API the comparison parameter is `to`:

```bash
curl -s -H "X-Satay-Token: $TOKEN" \
  'http://127.0.0.1:8787/runs/<source-run-id>/compare?to=<fork-run-id>'
```

Fork accepts terminal runs only: completed, failed, or cancelled. A fork shares blob files with its
source run, and there is no blob garbage collection.

## Without A Browser

```console
$ satay runs show 1e909aadfbd34ba2b43a0c6a3bd657d2 --data-dir .satay-demo
Run 1e909aadfbd34ba2b43a0c6a3bd657d2 — 28 event(s)
    1  2026-01-01T00:00:00+00:00  WorkflowCreated  workflow=morning_digest code_version=src:21b941c439f0af5c
    2  2026-01-01T00:00:00+00:00  TaskScheduled  task=fetch_feed key=source-hn
    3  2026-01-01T00:00:00+00:00  TaskAttemptStarted  task=fetch_feed key=source-hn attempt=1
    4  2026-01-01T00:00:00+00:00  TaskCompleted  task=fetch_feed key=source-hn
⚡   5  2026-01-01T00:00:00+00:00  WorkflowResumed
```

Excerpted at five events. `satay runs show` is deliberately frozen at the V1 event subset: timer,
event, child-workflow, and fork events render as bare type lines. Studio is the surface that
renders everything, and that split is on purpose rather than unfinished.

For everything else the API offers, and the reasoning behind the loopback guard, see
[Studio and `satay dev`](../studio.md).

## Recap

- One run can carry a fan-out, a retry, a crash, a timer, an event, a child workflow, and recorded
  usage. Building one like it is the fastest way to learn the UI.
- `satay dev` is lock, store, worker, and server in one process on `127.0.0.1:8787`, and one
  instance per data directory.
- `--app your.module` is the difference between a journal viewer and a working dev stack. Without
  it, zero workflows are registered and nothing can be started or woken.
- Open the URL with its `?token=`. The page loads without one; the API does not.
- The API header is `X-Satay-Token`. `Authorization: Bearer` is a `401`.
- Use the execution tree for fan-outs, task detail for attempts and usage, and compare for forks.
- `satay runs show` covers the V1 event subset in text. Studio renders the rest.

That is the last recipe. Back to the [cookbook index](index.md), or on to
[Guarantees](../guarantees.md) for the contract behind all of it.
