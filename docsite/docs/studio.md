# Studio and `satay dev`

Satay Studio is a local web debugger over a journal. You get the run list, an event timeline with
the interruption marked, an execution tree, per-task detail, a call-by-call comparison of two runs,
and a fork control.

It lives in the optional extra:

```bash
pip install 'satay[studio]'
```

## Read this before you open the URL

Two things about the local surface confuse everybody, so they come first.

!!! danger "The `?token=` is part of the URL"

    `satay dev` mints a fresh random session token every time it starts and prints a URL with that
    token in the query string. **Open the printed URL, whole.** Visiting
    `http://127.0.0.1:8787/` on its own loads the Studio page and then every API call it makes
    comes back `401`, so you get an app shell that renders nothing and an error in the console.

!!! danger "The API header is `X-Satay-Token`, not `Authorization: Bearer`"

    Every request to the HTTP API must carry the token in an **`X-Satay-Token`** header. There is
    no bearer-token support. `Authorization: Bearer <token>` is rejected with the same `401` as
    sending nothing at all, which makes it look like your token is wrong when the header name is
    what is wrong.

Proof, against a live `satay dev`. The real token is 43 random URL-safe characters; it is written
as an obvious placeholder here so this page does not ship a high-entropy string that every secret
scanner in the world wants to flag.

```console
$ TOKEN=TOKEN_FROM_SATAY_DEV

$ curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8791/runs
401

$ curl -s -w '\n%{http_code}\n' -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8791/runs
{"detail":"missing or invalid session token"}
401

$ curl -s -w '\n%{http_code}\n' -H "X-Satay-Token: $TOKEN" http://127.0.0.1:8791/runs
{"runs":[{"run_id":"124282c0fc7a4f2692a5d80df8d025e3","workflow_name":"nightly_report","status":"completed", ...}]}
200
```

If you lost the URL but still have the token, Studio also reads it from
`localStorage["satay_token"]` and from a `window.__SATAY_TOKEN__` global, in that order after the
query string. Setting the localStorage key in devtools once beats re-copying the URL on every
restart:

```js
localStorage.setItem("satay_token", "TOKEN_FROM_SATAY_DEV")
```

The static Studio page itself is served unauthenticated, on purpose. The browser has to be able to
load the bundle before it has anywhere to put a token, so the guard sits on the API rather than on
the HTML.

## Starting it

```bash
satay dev --app mypkg.workflows                 # import your workflows, then boot
satay dev --app mypkg.flows --app mypkg.jobs    # repeatable
satay dev                                       # ./.satay on 127.0.0.1:8787
satay dev --port 9000                           # a different port; 0 picks an ephemeral one
satay dev --data-dir .satay-demo                # a different journal
satay dev --host 127.0.0.1                      # loopback only, and it will not budge
```

```console
$ satay dev --app userapp --port 8791
app modules (--app): userapp
registered: 2 workflows (await_approval, nightly_report); 1 task (render)
policies: effect_safety=warn, nondeterminism=strict, version_mismatch=warn
INFO:     Started server process [788330]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:8791 (Press CTRL+C to quit)
Satay Studio:  http://127.0.0.1:8791/?token=TOKEN_FROM_SATAY_DEV
  control/read API on http://127.0.0.1:8791  (session token required)
  press Ctrl-C to stop
```

Startup order is lock, store, worker, HTTP server. Shutdown is the exact reverse, then the lock is
released.

That first step is an exclusive advisory lock on `<data-dir>/dev.lock`. A second `satay dev` on the
same directory is refused rather than allowed to race the single-writer journal:

```console
$ satay dev --port 8792
error: another satay dev process holds the lock on /home/you/project/.satay/dev.lock (pid=788330);
refusing to start a second writer on the same data directory, which would race the single-writer
journal into corruption (ADR-0017/Q54). Stop that process, or run with a different --data-dir.
```

The lock uses POSIX `flock`, so on Windows it degrades to a no-op and this protection is not there.

!!! info "`--app` landed after `0.1.0a1`"

    The published alpha has no `--app` flag, so its dev stack cannot import your workflows and
    cannot start or wake them. See the [install note](tutorial/index.md#install) for how to get a
    build that can.

## Telling `satay dev` where your workflows live

A workflow only exists as far as the runtime is concerned once its `@satay.workflow` decorator has
run, which happens when Python imports the module it is written in. So the dev stack has to import
your code before it can do anything with it. `--app` is how you say which modules those are:

```bash
satay dev --app mypkg.workflows
```

Repeat the flag for several modules. To stop retyping it, put the list in your `pyproject.toml` and
run a bare `satay dev`:

```toml
[tool.satay]
app = ["mypkg.workflows", "mypkg.jobs"]
```

An explicit `--app` replaces that list rather than adding to it, so the command line always wins.

With the modules loaded, one process does everything. `POST /runs` can start your workflows:

```console
$ curl -s -w '\n%{http_code}\n' -X POST -H "X-Satay-Token: $TOKEN" \
    -H 'Content-Type: application/json' \
    -d '{"workflow":"nightly_report","input":"2026-07-31"}' \
    http://127.0.0.1:8791/runs
{"run_id":"124282c0fc7a4f2692a5d80df8d025e3","status":"running"}
202

$ curl -s -H "X-Satay-Token: $TOKEN" http://127.0.0.1:8791/runs
{"runs":[{"run_id":"124282c0fc7a4f2692a5d80df8d025e3","workflow_name":"nightly_report","status":"completed", ...}]}
```

`202` then `completed`: the write was accepted onto the command queue, the worker picked it up on
its next tick, and the run finished. The poll loop wakes your runs that are parked on a timer or an
event the same way. A run parked by some other process (a script you ran an hour ago and quit) gets
woken by `satay dev` just the same, because the journal is the only state that matters.

The boot always tells you what it registered, and the failures are loud:

```console
$ satay dev --app userapp
app modules (--app): userapp
registered: 2 workflows (await_approval, nightly_report); 1 task (render)

$ satay dev --app userapp.wokflows
error: --app module 'userapp.wokflows' was not found (no module named 'userapp.wokflows').
It must be importable from /home/you/project — either installed into this environment, or
a package/module directory here.

$ satay dev --app brokenapp
error: --app module 'brokenapp' raised RuntimeError while importing: DATABASE_URL is not set
```

Both of those exit `2` without touching the data directory. And a bare `satay dev` with nothing to
import says so rather than looking healthy:

```console
$ satay dev
app modules: none (no --app, no [tool.satay] app in pyproject.toml)
registered: 0 workflows; 0 tasks
  warning: 0 workflows registered — this process can serve Studio and read the journal, but it
  cannot start a run or wake one parked on a timer or event. Pass --app your.module to import
  your workflows.
policies: effect_safety=warn, nondeterminism=strict, version_mismatch=warn
```

That is a perfectly good way to run it if all you want is to look at a journal. It is not a way to
run anything.

!!! note "Modules are imported, not exec'd from a path"

    `--app` takes dotted module paths, not filenames. Your project directory is added to the
    **end** of `sys.path`, so a module in the directory you ran from is importable without
    installing anything, and a local `queue.py` cannot shadow the stdlib's, because the stdlib
    resolves first. If your code lives somewhere else entirely, install it (`pip install -e .`) or
    set `PYTHONPATH`.

## The tour

Start from `make demo` in a clone of the repository, which leaves a completed run with an
interruption in it and then opens Studio on that journal.

**Runs.** Every run with its workflow name, status, creation time, and a version-mismatch chip when
the code has moved since the run was stamped. Click one to open it.

**Timeline.** The event log, in order, with the `⚡` on `WorkflowResumed` marking where the process
died. Expand an event for its payload. Each event also offers "fork before here", which is the
write below.

**Execution tree.** The run as a tree of durable calls rather than a flat log: tasks, `map` fan-out
grouped under its parent call, and child workflows nested where they were started.

**Task detail.** One durable call in full: its identity, every attempt with its error and backoff,
the recorded input and output, and any model usage a task self-reported through
`ctx.record_model_usage(...)`.

**Compare.** Two runs side by side, matched by durable-call identity rather than by sequence number,
so you can see which calls a fork replayed from the journal and which it re-ran. Fork a run and then
compare it against its source: the lineage chip on the run header wires that up in one click.

Studio polls for freshness rather than holding a socket open, so a run that advances while you are
watching updates on its own.

### Fork

Forking is the one write Studio makes. It copies the source run's journal up to a chosen event into
a brand new run, then drives that new run forward under whatever your code says *now*. The source
run is untouched.

This is the answer to "I need to change a workflow that has runs in flight". Satay has no automatic
migration across code versions. Let the old runs drain, or fork them.

Forking accepts terminal runs only: completed, failed, or cancelled. A fork also shares blob files
with its source run, which matters because there is no blob garbage collection.

## The HTTP API

Same base URL as Studio, same `X-Satay-Token` header on every request. Writes return `202` and land
on a command queue the worker drains on its next tick, so a write is accepted rather than applied
inline. That is the single-writer discipline. Reads go straight to the store and never block on the
worker.

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

`POST /runs` can only start a workflow the serving process has registered, so it starts whatever you
passed to [`--app`](#telling-satay-dev-where-your-workflows-live), and nothing at all if you passed
nothing.

Reading a timeline:

```console
$ curl -s -H "X-Satay-Token: $TOKEN" \
    http://127.0.0.1:8791/runs/124282c0fc7a4f2692a5d80df8d025e3/timeline | python -m json.tool
{
    "run_id": "124282c0fc7a4f2692a5d80df8d025e3",
    "workflow_name": "nightly_report",
    "status": "completed",
    "interrupted": false,
    "version_mismatch": {
        "stamped": "src:6265e8fbbc0453b4",
        "current": "src:6265e8fbbc0453b4",
        "mismatch": false
    },
    "forked_from": null,
    "events": [
        {
            "seq": 1,
            "event_id": "fc0a5a3cb2ac4677bb8f7391256ba057",
            "type": "WorkflowCreated",
            "ts": "2026-07-31T07:49:29.308638+00:00",
            "is_interruption": false,
            "payload": {
                "workflow_name": "nightly_report",
                "input_ref": "2026-07-31",
                "code_version": "src:6265e8fbbc0453b4"
            }
        }
    ]
}
```

Every read passes through the [redactor](guarantees.md#redaction) on the way out.

## Why the guard is shaped this way

A browser-reachable service on a predictable `127.0.0.1` port is not safe simply because it is
local. Any page you have open can POST to it, and DNS rebinding can defeat same-origin well enough
to read your runs back. So there are three checks:

- a **per-session token** in `X-Satay-Token`, regenerated on every `satay dev`;
- an **`Origin` and `Host` allow-list**, where a cross-origin `Origin` or a `Host` that does not
  resolve to loopback is a `403`. A missing `Origin`, which is what a non-browser client sends, is
  allowed;
- a **loopback-bind refusal**. Asking for `--host 0.0.0.0` raises rather than binding.

That last one is the honest boundary: this guard is proportionate protection for a developer tool on
a laptop, and it is not network authentication. Do not put it on a shared host.

## The code-version stamp

Every run records a code version at creation. The order is `git rev-parse HEAD` where a git checkout
is available, then `SATAY_CODE_VERSION` if you set it, then a hash of your source (you will see
`src:...` prefixes in that case).

On resume Satay compares the stamp against the current version and applies its **own** policy,
`version_mismatch`, set with `SATAY_VERSION_MISMATCH` and separate from `effect_safety` since
[ADR-0023](decisions.md):

| Mode | Behaviour |
| --- | --- |
| `off` | Silent. |
| `warn` | Logs and continues. The default. |
| `strict` | Raises `VersionMismatchError`. |

`satay dev` resolves that policy, along with `effect_safety` and `nondeterminism`, from the
environment and prints all three at boot, so `SATAY_VERSION_MISMATCH=strict satay dev --app ...`
does what it says.

You will see mismatch chips in Studio a lot, and often harmlessly. A `satay dev` started from a
different working directory than the one that wrote the run computes a different source hash, so the
chip lights up even though nothing about your code changed. Read it as a prompt to check, not as a
verdict.
