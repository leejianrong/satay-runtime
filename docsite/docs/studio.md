# Studio and `satay dev`

Satay Studio is a local web debugger over a journal. You get the run list, an event timeline
with the interruption marked, an execution tree, per-task detail, a call-by-call comparison of
two runs, and a fork control.

It lives in the optional extra:

```bash
pip install 'satay[studio]'
```

## Read this before you open the URL

Two things about the local surface confuse everybody, so they come first.

!!! danger "The `?token=` is part of the URL"

    `satay dev` mints a fresh random session token every time it starts and prints a URL with
    that token in the query string. **Open the printed URL, whole.** Visiting
    `http://127.0.0.1:8787/` on its own loads the Studio page and then every API call it makes
    comes back `401`, so you get an app shell that renders nothing and an error in the console.

!!! danger "The API header is `X-Satay-Token`, not `Authorization: Bearer`"

    Every request to the HTTP API must carry the token in an **`X-Satay-Token`** header. There
    is no bearer-token support. `Authorization: Bearer <token>` is rejected with the same `401`
    as sending nothing at all, which makes it look like your token is wrong when the header
    name is what is wrong.

Proof, against a live `satay dev`. The real token is 43 random URL-safe characters; it is
written as a placeholder here so this page does not ship a high-entropy string that every
secret scanner in the world wants to flag.

```console
$ TOKEN=THE_TOKEN_SATAY_DEV_PRINTED

$ curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8791/runs
401

$ curl -s -w '\n%{http_code}\n' -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8791/runs
{"detail":"missing or invalid session token"}
401

$ curl -s -w '\n%{http_code}\n' -H "X-Satay-Token: $TOKEN" http://127.0.0.1:8791/runs
{"runs":[{"run_id":"cde47e8f...","workflow_name":"with_retries","status":"completed", ...}]}
200
```

If you lost the URL but still have the token, Studio also reads it from
`localStorage["satay_token"]` and from a `window.__SATAY_TOKEN__` global, in that order after
the query string. Setting the localStorage key in devtools once beats re-copying the URL on
every restart:

```js
localStorage.setItem("satay_token", "THE_TOKEN_SATAY_DEV_PRINTED")
```

The static Studio page itself is served unauthenticated, on purpose. The browser has to be able
to load the bundle before it has anywhere to put a token, so the guard sits on the API rather
than on the HTML.

## Starting it

```bash
satay dev                                  # ./.satay on 127.0.0.1:8787
satay dev --port 9000                      # a different port; 0 picks an ephemeral one
satay dev --data-dir .satay-demo           # a different journal
satay dev --host 127.0.0.1                 # loopback only, and it will not budge
```

```console
$ satay dev --port 8791
INFO:     Started server process [303058]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:8791 (Press CTRL+C to quit)
Satay Studio:  http://127.0.0.1:8791/?token=THE_TOKEN_SATAY_DEV_PRINTED
  control/read API on http://127.0.0.1:8791  (session token required)
  press Ctrl-C to stop
```

Startup order is lock, store, worker, HTTP server. Shutdown is the exact reverse, then the lock
is released.

That first step is an exclusive advisory lock on `<data-dir>/dev.lock`. A second `satay dev` on
the same directory is refused rather than allowed to race the single-writer journal:

```console
$ satay dev --port 8792
error: another satay dev process holds the lock on /path/.satay/dev.lock (pid=301603);
refusing to start a second writer on the same data directory, which would race the
single-writer journal into corruption (ADR-0017/Q54). Stop that process, or run with
a different --data-dir.
```

The lock uses POSIX `flock`, so on Windows it degrades to a no-op and this protection is not
there.

## `satay dev` cannot run your workflows

This is worth being blunt about. `satay dev` never imports your code. Its workflow registry is
empty, so its poll loop cannot wake a run of *your* workflow that is parked on a timer or an
event. It can read any journal, and it can cancel, fork, and deliver events into the inbox, but
the actual driving of your workflows happens in your process.

The working shape is two processes: your application (which registers the workflows and runs a
[`TimerEventWorker`](primitives.md#running-the-worker)) writing the journal, and `satay dev`
pointed at the same data directory for looking at it. That is exactly what `make demo` does, and
why it runs the demo script first and starts Studio second.

## The tour

Start from `make demo`, which leaves a completed run with an interruption in it.

**Runs.** Every run with its workflow name, status, creation time, and a version-mismatch chip
when the code has moved since the run was stamped. Click one to open it.

**Timeline.** The event log, in order, with the `⚡` on `WorkflowResumed` marking where the
process died. Expand an event for its payload. Each event also offers "fork before here", which
is the write below.

**Execution tree.** The run as a tree of durable calls rather than a flat log: tasks, `map`
fan-out grouped under its parent call, and child workflows nested where they were started.

**Task detail.** One durable call in full: its identity, every attempt with its error and
backoff, the recorded input and output, and any model usage a task self-reported through
`ctx.record_model_usage(...)`.

**Compare.** Two runs side by side, matched by durable-call identity rather than by sequence
number, so you can see which calls a fork replayed from the journal and which it re-ran. Fork a
run and then compare it against its source: the lineage chip on the run header wires that up in
one click.

Studio polls for freshness rather than holding a socket open, so a run that advances while you
are watching updates on its own.

### Fork

Forking is the one write Studio makes. It copies the source run's journal up to a chosen event
into a brand new run, then drives that new run forward under whatever your code says *now*.
The source run is untouched.

This is the answer to "I need to change a workflow that has runs in flight". Satay has no
automatic migration across code versions. Let the old runs drain, or fork them.

Forking accepts terminal runs only: completed, failed, or cancelled. A fork also shares blob
files with its source run, which matters because there is no blob garbage collection.

## The HTTP API

Same base URL as Studio, same `X-Satay-Token` header on every request. Writes return `202` and
land on a command queue the worker drains on its next tick, so a write is accepted rather than
applied inline. That is the single-writer discipline. Reads go straight to the store and never
block on the worker.

| Method | Path | Does |
| --- | --- | --- |
| `POST` | `/runs` | Start a run. Body: `workflow`, `input`, `idempotency_key`, `run_id`. |
| `POST` | `/runs/{run_id}/cancel` | Append `WorkflowCancelled` and halt the run. |
| `POST` | `/runs/{run_id}/events` | Deliver into the inbox. Body: `event_type`, `key`, `payload`. |
| `POST` | `/runs/{run_id}/fork` | Fork from a prefix. Body: `fork_point_seq`. |
| `GET` | `/runs` | The run list. |
| `GET` | `/runs/{run_id}/timeline` | Events with status, interruption flag, version mismatch. |
| `GET` | `/runs/{run_id}/tree` | The durable-call tree. |
| `GET` | `/runs/{run_id}/tasks/{identity}` | One call: attempts, input, output, usage. |
| `GET` | `/runs/{run_id}/compare?to={other_run_id}` | Call-by-call comparison. |

`POST /runs` has the same registry limitation as the worker: it can only start a workflow the
serving process has registered, which for a plain `satay dev` is none of yours.

Reading a timeline:

```console
$ curl -s -H "X-Satay-Token: $TOKEN" \
    http://127.0.0.1:8791/runs/3bb1ae9ec93846b2a811e76746705da7/timeline | python -m json.tool
{
    "run_id": "3bb1ae9ec93846b2a811e76746705da7",
    "workflow_name": "guarded",
    "status": "completed",
    "interrupted": false,
    "version_mismatch": {
        "stamped": "src:0e6eca458d2b3323",
        "current": "src:e3b0c44298fc1c14",
        "mismatch": true
    },
    "forked_from": null,
    "events": [
        {
            "seq": 1,
            "event_id": "3997e127483249218b611e32d40ca1bc",
            "type": "WorkflowCreated",
            "ts": "2026-07-30T19:42:48.099816+00:00",
            "is_interruption": false,
            "payload": {
                "workflow_name": "guarded",
                "input_ref": 1999,
                "code_version": "src:0e6eca458d2b3323"
            }
        }
    ]
}
```

Every read passes through the [redactor](guarantees.md#redaction) on the way out.

## Why the guard is shaped this way

A browser-reachable service on a predictable `127.0.0.1` port is not safe just because it is
local. Any page you have open can POST to it, and DNS rebinding can defeat same-origin well
enough to read your runs back. So there are three checks:

- a **per-session token** in `X-Satay-Token`, regenerated on every `satay dev`;
- an **`Origin` and `Host` allow-list**, where a cross-origin `Origin` or a `Host` that does not
  resolve to loopback is a `403`. A missing `Origin`, which is what a non-browser client sends,
  is allowed;
- a **loopback-bind refusal**. Asking for `--host 0.0.0.0` raises rather than binding.

That last one is the honest boundary: this guard is proportionate protection for a developer
tool on a laptop, and it is not network authentication. Do not put it on a shared host.

## The code-version stamp

Every run records a code version at creation. The order is `git rev-parse HEAD` where a git
checkout is available, then `SATAY_CODE_VERSION` if you set it, then a hash of your source (you
will see `src:...` prefixes in that case). On resume Satay compares the stamp against the current
version and applies the same policy as everything else: `warn` logs and continues, `strict`
raises `VersionMismatchError`, `off` says nothing.

You will see mismatch chips in Studio a lot, and often harmlessly. A `satay dev` started from a
different working directory than the one that wrote the run computes a different source hash, so
the chip lights up even though nothing about your code changed. Read it as a prompt to check,
not as a verdict.
