# Satay Runtime

Satay is a **local-first durable-execution runtime** for async Python. You write
ordinary `async def` workflows and tasks; Satay records every durable call to an
append-only journal and, on crash, **replays the workflow from the top** — reusing
recorded results and re-executing only what never finished. No external
infrastructure: it runs in one process over SQLite.

```python
import satay

@satay.task()
async def charge(amount: int) -> str: ...

@satay.workflow
async def checkout(order):
    receipt = await charge(order["total"])
    return receipt
```

The debugger — **Satay Studio**, a local web UI over an HTTP read API — ships in the
optional `satay[studio]` extra, so applications that embed Satay never carry the
FastAPI/uvicorn/JS stack into production.

**Documentation: [leejianrong.github.io/satay-runtime](https://leejianrong.github.io/satay-runtime/)**
— the tutorial, the cookbook, the Studio guide, and the honest account of what the runtime
does not do.

## Status

**The MVP is built and the full suite is green (V1–V8).** Trust the code over the docs:
`docs/` describes the intended full system, and where docs and code disagree the code
wins. `uv run pytest -q` prints the current test count.

What works today:

- **Durable core + replay.** Workflows replay from the top against an append-only
  SQLite journal (`PRAGMA user_version` schema, forward-only migrations, WAL); recorded
  results are reused and only unfinished work re-executes.
- **Execution guarantees.** Retries with capped exponential backoff and jittered delays
  off an injected clock, at-least-once task execution, runtime-derived idempotency keys
  readable inside a task body, replay nondeterminism detection (strict by default — a
  divergent replay raises rather than returning a plausible wrong answer), and a separate
  `effect_safety` policy that guards retryable side-effecting tasks.
- **Time and events.** Durable `sleep`, `wait_for_event`/`send_event` over a persistent
  inbox, and a timer + event poll loop with FIFO delivery and event-wins-over-timeout.
  `async with satay.run_app() as store:` gives a plain script that loop — journal open,
  worker running, both torn down on exit — with no `satay dev` and no optional extra.
- **Composition.** `map`, `gather`, and `start_child`, each item a keyed durable call, so
  a crash mid-fan-out resumes with completed items reused and only unresolved items re-run.
- **Control plane.** An HTTP control + read API (start, cancel, send event, fork; run
  list, timeline, tree, task detail, compare) with a redactor applied to every read and a
  loopback/token security guard.
- **Studio.** The Satay Studio SPA ships as a built bundle served by the same process.
- **Forking and versioning.** Fork a run from a prefix, compare two runs call-by-call, and
  a code-version stamp with a strict/warn/off mismatch policy on resume.
- **`satay dev`.** One command brings up the lock, store, worker, and Studio server, and
  tears them down in reverse.
- **Payload spill.** Encoded payloads over 256 KiB spill to content-addressed blob files
  transparently on write and rehydrate on read.

Deliberate MVP gaps, so the honesty survives contact:

- **No blob GC.** No run deletion and no compaction; blobs accumulate under `./.satay/`
  and removal is manual (ADR-0004). A future GC has to be reference-aware, since forks
  share blobs with their source run.
- **Fan-out is fail-fast by default.** The first failure raises and sibling results are
  discarded. `map` and `gather` take `return_exceptions=True` for collect mode, which
  settles every item and records a terminal `TaskFailed` per collected failure (ADR-0027,
  superseding ADR-0020). `start_child` has no such flag — collect it as a `gather` member.
- **`satay runs show` is frozen at the V1 event subset** (ADR-0016). Timer, event,
  cancellation, and fork events render as bare type lines; Studio covers the rest.
- **Fork only accepts terminal runs** (ADR-0004) — completed, failed, or cancelled.
- **One process, one writer.** No PostgreSQL backend, no multi-worker or distributed
  execution. The cross-process data-dir lock is POSIX `flock` and only `satay dev` takes it.
- **Async only.** Sync (non-async) workflows and tasks are unsupported.
- **Nondeterminism detection is runtime-only** and compares the durable-call schedule,
  not arguments; there is no static analysis of workflow bodies, and no automatic
  migration of long-running workflows across code versions.
- **Windows is best-effort** and SQLite on network filesystems is unsupported (ADR-0019).

See `docs/` for the specs and `CLAUDE.md` for the build brief.

## Requirements

- Python **3.12 or 3.13**
- [`uv`](https://docs.astral.sh/uv/) for environment and dependency management
- Linux/macOS first-class; Windows best-effort (SQLite on local disk only)

## Dev quickstart

```bash
uv sync                              # create the venv, install deps + dev group
uv run ruff check .                  # lint
uv run ruff format --check .         # format check
uv run mypy src                      # type-check (strict)
uv run pytest tests/unit -q          # unit tests
```

The integration and e2e tests that cover Studio and the HTTP API need the `studio` extra;
without it they skip themselves and the reported count silently drops:

```bash
uv sync --extra studio               # then run the full suite
uv run pytest -q
```

Or via the Makefile:

```bash
make dev      # uv sync
make check    # ruff + mypy
make test     # unit tests
make ci       # everything CI runs
```

Install the pre-push hook (runs the cheap gates before every push):

```bash
make install-hooks            # or: ./scripts/install-hooks.sh
```

## Layout

```
src/satay/        runtime core (pure Python, near-zero dependencies)
  api/            author decorators, primitives, run handle, TaskContext
  replay/         replay engine, identity, nondeterminism
  journal/        event model + Store seam + codec
  executor/       TaskExecutor seam + LocalTaskExecutor
  timers/         timer + event poll loop
  control/        HTTP control + read API   (satay[studio])
  versioning/     code-version stamper
  blobs/          payload spill
  devstack/       `satay dev` orchestrator  (satay[studio])
  testing/        fault injection, manual clock, seeded RNG, fixtures
  cli/            core argparse CLI (`satay runs show`)
tests/{unit,integration,e2e}/
docs/             specs + ADRs
```

## License

Apache-2.0. See [LICENSE](LICENSE).
