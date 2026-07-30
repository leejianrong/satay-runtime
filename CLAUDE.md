# CLAUDE.md — agent brief for Satay Runtime

Satay is a **local-first durable-execution runtime** for async Python: `async def`
workflows/tasks whose durable calls are recorded to an append-only journal and
**replayed from the top** on crash, reusing recorded results. One process, SQLite,
no external infra. The debugger (Studio) ships in the optional `satay[studio]` extra.

## Build status — trust the code over the docs

**V1–V8 are merged; the MVP is built and green at 246 tests.** Nothing in `src/` raises
`NotImplementedError` any more. The `docs/` describe the *intended* full system; where
docs and code disagree, the code is the truth for what exists today — verify before you
believe a docstring, including this section.

What is real now:

- **Durable core + replay** (`replay/`): replay from the top over an append-only journal,
  reusing recorded results; identity resolution and nondeterminism detection.
- **SQLite journal** (`journal/`): `SQLiteStore` on raw stdlib `sqlite3`, versioned by
  `PRAGMA user_version` with forward-only migrations, WAL, per-run async writer lock.
- **Execution guarantees** (`executor/`): retries with capped exponential backoff and
  full-jitter delays off the injected clock and RNG, at-least-once execution,
  runtime-derived idempotency keys, and `effect_safety` (strict/warn/off) guarding
  retryable `side_effect=True` tasks that are not declared `idempotent=True`.
- **Time and events** (`timers/`): durable `sleep`, `wait_for_event`/`send_event` over a
  persistent inbox, and the timer + event poll loop (FIFO, event-wins-over-timeout).
- **Composition** (`api/primitives.py`): `map`/`gather`/`start_child` as keyed durable
  calls, with partial-completion recovery mid-fan-out.
- **Control plane** (`control/`): HTTP control + read API, a `Redactor` forced on every
  read, and a loopback/token `SecurityPolicy`; writes serialize through a `CommandQueue`.
- **Studio** (`_studio_assets/`): the built SPA bundle, served at `/` by the V5 process.
- **Fork, compare, versioning** (`control/commands.py`, `versioning/`): prefix fork, run
  comparison by durable-call identity, code-version stamp + mismatch policy on resume.
- **`satay dev`** (`devstack/`): lock → store → worker → server, torn down in reverse.
- **Payload spill** (`blobs/`): `SPILL_THRESHOLD_BYTES = 262144`; payloads over 256 KiB
  go to content-addressed blob files, transparent on write and read.
- **Test seam** (`testing/`): `ManualClock`, `SeededRng`, `FaultInjector`, pytest fixtures.

Deliberate MVP gaps — do not "fix" these without a card:

- **No blob GC**, no run deletion, no compaction (ADR-0004). Forks share blobs with their
  source run, so any future GC must be reference-aware.
- **Fan-out is fail-fast only** — no collect / `return_exceptions` mode (ADR-0020).
- **`satay runs show` is frozen at the V1 event subset** (ADR-0016); Studio covers the
  rest. Post-V1 events render as bare type lines by design.
- **Fork accepts terminal runs only** (ADR-0004).
- **No PostgreSQL, no multi-worker, no distributed execution.** One process, one writer.
- **Async only** — no sync workflows/tasks.
- **Nondeterminism detection is runtime-only** and compares the durable-call schedule, not
  arguments; no static analysis, no automatic cross-version workflow migration.
- **Windows is best-effort** (the cross-process data-dir lock is POSIX `flock` and
  degrades to a no-op elsewhere); SQLite on network filesystems is unsupported (ADR-0019).

## Commands

```bash
uv sync                        # env + deps (installs the dev group)
uv run ruff check .            # lint
uv run ruff format .           # format (add --check in CI/hooks)
uv run mypy src                # type-check, strict
uv run pytest tests/unit -q    # unit tests
uv run pytest tests/integration --collect-only -q   # import-hygiene guard
uv sync --extra studio && uv run pytest -q          # full suite: 246 passed
```

The full suite needs the `studio` extra — without it the FastAPI/Studio tests
`importorskip` themselves away and the count silently drops.

Shortcuts: `make check` (ruff + mypy), `make test` (unit), `make ci` (all).
Install the pre-push hook with `make install-hooks`; bypass with `git push --no-verify`.

## Workflow conventions

- **`main` is protected — PR-only, never push to `main`.**
- **Branch per slice:** `git switch -c feat/<slice>` off `origin/main`; open a PR.
- Run the cheap gates locally (`make ci`) before pushing; the pre-push hook mirrors
  them.
- Commit trailer: `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`.

## Module map (mirrors ARCHITECTURE §1)

```
src/satay/
  api/         author decorators, 5 primitives, run handle, TaskContext   (A1)
  replay/      replay engine, identity resolver, nondeterminism           (A2)
  journal/     event model (frozen dataclasses), Store seam, codec        (A3)
  executor/    TaskExecutor seam, LocalTaskExecutor, retry/backoff         (A4)
  timers/      timer + event poll loop, event inbox                        (A5)
  control/     HTTP control + read API — satay[studio] only               (A7/A8)
  versioning/  code-version stamper + mismatch policy                      (A10)
  blobs/       payload spill to local files                                (A3.4)
  devstack/    `satay dev` orchestrator — satay[studio] only               (A9)
  testing/     fault injection, manual clock, seeded RNG, fixtures         (ADR-0011)
  cli/         core argparse CLI (`satay runs show`)                       (U1)
```

Public surface (re-exported from `satay/__init__.py`): `workflow`, `task`, `start`,
`sleep`, `wait_for_event`, `send_event`, `map`, `gather`, `start_child`,
`TaskContext`, `RunHandle`.

## The core-dependency boundary (do not cross)

The runtime core is **pure-Python, near-zero third-party deps** (ADR-0013/0016).
Hold this line — it is the product's main packaging promise:

- **No Pydantic in the core.** It is *duck-typed*: call `model_validate` only when a
  declared return type provides it, behind an optional import.
- **CLI uses `argparse`.** Typer + `satay dev` live **only** in `satay[studio]`.
- **FastAPI/uvicorn** live **only** in `satay[studio]`.
- **Events are stdlib frozen dataclasses;** persistence is **raw SQL over stdlib
  `sqlite3`, no ORM**.
- **Retry/backoff hand-rolled** via the injected clock (no `tenacity`); stdlib
  `logging`; no `dulwich` (git binary → source-hash fallback).
- `tests/integration/test_import_hygiene.py` enforces that importing the core pulls
  none of `fastapi/uvicorn/pydantic/typer/click`. Keep it green.

## Test seam (ADR-0011)

The **primary seam is the public API** driving real workflows against a **temp SQLite
store**, with injected determinism controls: the **manual clock** (`ManualClock`), the
**seeded RNG** (`SeededRng`), and the **fault-injection hook** (`FaultInjector` —
crash or stall after a named journal event). Assert **observable outcomes** (result,
status, journal, execution-count marker), never private replay internals. Fixtures are
in `satay.testing.fixtures` (loaded as a pytest plugin from `tests/conftest.py`).

## Persistence (ADR-0017)

Data lives under a project-local `./.satay/` (override `--data-dir` /
`SATAY_DATA_DIR`); schema is versioned with `PRAGMA user_version`, forward-only
migrations. See `satay/config.py` for the layout and `satay/journal/store.py` for the
live `SQLiteStore` (schema v3, WAL, refuses a DB newer than the code).

## Pointers

- `docs/ARCHITECTURE.md` §1–§2 — structure + system model
- `docs/CONTEXT.md` — glossary + decision register D1–D22
- ADRs of record: 0011 (test seam), 0012 (co-hosting/single-writer), 0013 (packaging),
  0015 (toolchain), 0016 (core deps), 0017 (persistence), 0019 (platform/release)
- `docs/SLICE-V*.md` — per-slice scope; V1 is the two-task crash-recovery headline
