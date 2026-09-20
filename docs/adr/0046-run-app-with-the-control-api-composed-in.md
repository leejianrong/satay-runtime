# ADR-0046 — `satay.control.run_app`: the control/read API composed into a `run_app`-shaped block

- **Status:** Accepted
- **Date:** 2026-09-20
- **Deciders:** Jian (leejianrong2@gmail.com)

Extends [ADR-0030](0030-run-app-and-the-parked-result.md) (`satay.run_app`) and
[ADR-0012](0012-api-cohosting-and-single-writer.md) (API co-hosting, single writer).
Reuses the exact worker/queue/server composition [ADR-0024](0024-dev-stack-app-module-loading.md)'s
`DevStack` already proves, scoped differently. Does not touch the core-dependency
boundary (ADR-0013/0016), the single-writer model, or `satay.run_app`'s own existing
contract — this is a new, additive entry point, not a change to either.

## Context

cuttlefish-agent — satay's first real external tenant (CLAUDE.md's own "Direction"
section) — needs a running task to be externally steerable: an operator sends a message
that redirects a coding agent's work while it is still running, not just reads its
history afterward. The mechanism is already public and already correct:
`satay.wait_for_event`/`satay.send_event`, exactly as ADR-0021 and the
`timers-events.md` cookbook page already describe them. The gap is narrower than a
missing primitive — it is that nothing external can reach a `cuttlefish run` process
while it runs.

`cuttlefish run` is one blocking CLI invocation: `async with satay.run_app() as store:
... await handle.result()`. ADR-0030 gave it a poll loop with nothing to talk to it.
The only thing in this codebase that *can* be talked to externally — the control/read
HTTP API (ADR-0009, ADR-0012, ADR-0014) — only ever runs as part of `satay dev`
(`DevStack`, ADR-0024): a whole standalone process that imports the caller's workflow
module by `--app` name, takes an exclusive advisory lock meant to protect a long-lived
interactive session, mounts Studio, and runs until `Ctrl-C`. None of that fits a caller
that has already imported its own workflow, already calls `satay.start` itself, and
wants the process to end when its own work ends — exactly what `run_app()` already
gives, minus the one thing an external actor needs: a way in.

The two are not actually different engines. `TimerEventWorker` — the same worker class
`run_app()` already uses — has taken an optional `commands: CommandQueue | None`
parameter announced right there in ADR-0012's original single-writer design (V5); `run_app()`
just never constructs one or wires an HTTP server to it. `DevStack.start()` is nothing
more than: open a store, build a `CommandQueue`, hand it to `TimerEventWorker`, and start
`satay.control.server`'s FastAPI/uvicorn app over the same store and queue. Every piece
already exists and is already tested (`tests/e2e/test_control_http.py`,
`tests/e2e/test_devstack_app.py`). What is missing is a *third* shape at this
composition's actual grain: `run_app()`'s single-block, no-`--app`-loading, ends-when-
you're-done ergonomics, with the control API attached for the block's own lifetime.

ADR-0030's own Consequences already named "two ways to run a poll loop, on purpose" as
the deliberate state of the world. This ADR makes a third, for the same underlying
reason ADR-0030 gave for the first two: a real caller hit a gap neither shape actually
covers.

## Decision

**A new function, `satay.control.run_app`, not a parameter on `satay.run_app`.**
Changing `satay.run_app`'s existing return type (`AsyncIterator[Store]`) conditionally on
a flag would either break every existing `async with satay.run_app() as store:` caller's
type, or need an `@overload` pair for what is fundamentally a studio-only capability
living in the core. Since starting the control API always needs FastAPI/uvicorn, it can
never belong in `satay.run_app` itself (ADR-0013/0016's core-dependency boundary) — it
belongs beside `create_app`/`serve`, in `satay.control`, imported lazily the same way.

```python
async with satay.control.run_app() as app:
    handle = satay.start(my_workflow, "input", store=app.store)
    print(f"steer it at {app.base_url}, token {app.token}")
    print(await handle.result())
```

`satay.control.run_app` takes every keyword `satay.run_app` takes (`data_dir`, `store`,
`interval`, `clock`, `rng`, `injector`, and the three policy overrides), plus the control
server's own (`host`, `port`, `token`, `allowed_origins`, `log_level` — the same
defaults `serve`/`DevStack` already use: loopback-only, ephemeral port, a generated
token). It yields a `ControlledApp` (a frozen dataclass: `store`, `base_url`, `token`),
not a bare `Store`, so a caller never has to guess which of the two functions they
called from what came back.

Internally it is exactly `DevStack.start()`'s composition — store, `CommandQueue`,
`TimerEventWorker(commands=queue)`, `create_app`, `uvicorn.Server` — reused, not
reinvented, over one `async with` block instead of a whole owned process. Teardown
mirrors `run_app()`'s own discipline: server, then worker, then the store this call
opened (never a caller-supplied one), in that order, on the normal path and the
exception path alike.

**The data-directory lock (ADR-0017/Q54) applies here too, when this call opens its own
store.** Two `satay.control.run_app()` blocks (or one of these and a `satay dev`) racing
the same `./.satay/` would be the exact corruption `DataDirLock` exists to prevent —
nothing about this being a narrower, block-scoped shape changes that risk. As with
`satay.run_app`, a caller-supplied `store=` skips the lock: that caller already owns the
store's lifetime and, in a test, is frequently `:memory:`, which has no directory to
lock in the first place.

**No `--app` module loading.** The caller already imported its own workflow to call
`satay.start` at all (identical reasoning to `satay.run_app`'s own ADR-0030 §1 "It does
not import your modules"). `satay dev`'s `--app` step exists only because that process
has no other way to populate the registry; this one does.

**Studio still mounts if the bundle is present, for free.** `create_app`'s
`_mount_studio` behaviour is untouched — a caller who happens to have `satay[studio]`'s
full bundle installed gets Satay Studio's own UI at `base_url`, not just the raw API,
with no code of its own. Not the point of this ADR, but a real, free side effect worth
naming: a consumer wanting a live view of a running task before it builds one of its
own can point a browser at `app.base_url`.

## Alternatives considered

| Option | Why not |
|--------|---------|
| A parameter on `satay.run_app` (`control_api=True`). | Forces a conditional return type on the one function every existing caller already depends on, purely to add a studio-only capability that structurally cannot live in the core import path anyway (ADR-0013/0016). |
| Tell cuttlefish-agent to use `satay dev` / `DevStack` directly. | Wrong grain: `DevStack` is a whole owned process (advisory lock semantics tuned for a long-lived interactive session, `--app` module loading, a `Ctrl-C` shutdown model). `cuttlefish run` is one task, one blocking invocation, already importing its own workflow — forcing it through `DevStack` would mean building a second process and a second store connection just to talk to the first, which is exactly the cross-process pattern ADR-0012's single-writer model does not support. |
| Give up on in-process composition and build real cross-process/multi-worker event delivery (the Postgres/multi-worker milestone, ARCHITECTURE §9 phases 2-3). | Unnecessary for the actual problem. The need was "get an event into a process that's already running," not "run many writers against one store." ADR-0025 already deferred Postgres/multi-worker past the agents-first launch for reasons that still hold; this ADR delivers the steering capability without touching that ordering at all. |
| Make `satay.control.run_app` a top-level primitive (`satay.run_app_with_control`). | Rejected on the same grounds ADR-0043 §2 gives for the agent loop: a studio-only capability advertised at the top level blurs which surface is the runtime core and which is built on it. One import away (`satay.control.run_app`) says plainly that this needs the extra. |

## Consequences

cuttlefish-agent (and any other embedding caller) gets a real, local, single-process way
to expose `start`/`cancel`/`send_event`/`fork` to something outside the process — a
second CLI invocation, a browser, Satay Studio itself — for exactly the lifetime of one
`async with` block, with no new engine capability and no change to the single-writer
model. `ARCHITECTURE` §9's phase ordering (PostgreSQL, then multi-worker) is untouched;
this is composition of existing, already-shipped parts, not a step on that ladder.

It also costs a maintenance seam that did not exist before: `DevStack.start()`/`stop()`
and `satay.control.run_app`'s teardown now describe the *same* lifecycle
(store → queue → worker → server, reverse on the way out) in two places rather than one.
Left as-is for this ADR rather than refactored into one shared helper underneath both —
`DevStack` additionally owns the advisory lock's always-on posture, `--app` loading, and
a `Ctrl-C` wait loop, which don't belong under `satay.control.run_app`'s own contract, and
the two implementations are short enough (roughly the size of `DevStack.start`/`stop`
combined) that a shared helper would trade one seam for another without a hard number
behind it. A third caller of this exact composition is the trigger to extract one.

`satay.control.run_app` is new studio-only surface with no test coverage yet from any
caller but the one that asked for it — the same accepted risk `run_app()`'s own ADR-0030
took for the core shape, and the reason this ADR asks for the same style of proof:
an e2e test driving a real workflow parked on `wait_for_event`, woken over real HTTP,
exactly like `tests/e2e/test_control_http.py`'s existing demo, plus the teardown-order
and lock-conflict tests `tests/unit/test_devstack.py` already models.
