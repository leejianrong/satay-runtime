# CLAUDE.md, agent brief for Satay Runtime

Satay is a local-first durable-execution runtime for async Python. `async def` workflows
and tasks record their durable calls to an append-only journal and replay from the top on
crash, reusing recorded results. Execution is at-least-once, with runtime-derived
idempotency keys. One process, SQLite, no external infra. The debugger (Studio) ships in
the optional `satay[studio]` extra.

## Trust the code over the docs

V1 to V8 are merged, `0.1.0` is released, and nothing in `src/` raises
`NotImplementedError`. The `docs/` describe the intended full system. Where docs and code
disagree the code is the truth, and that includes this file, so verify before you believe
a docstring.

No test counts here, because they go stale on the next PR that adds one. Run
`uv run pytest -q` for the number, and treat a red suite rather than a changed count as
the signal that something is wrong.

Two policies are easy to confuse and have nothing to do with each other:

- `nondeterminism` (strict/warn/off, **strict** by default) governs replay divergence. A
  divergent replay raises `NondeterminismError` (ADR-0022).
- `effect_safety` (strict/warn/off, **warn** by default) governs one check and no others:
  a retryable `side_effect=True` task that has not declared `idempotent=True`.

## Deliberate gaps, do not "fix" these without a card

- No blob GC, no run deletion, no compaction (ADR-0004). Forks share blobs with their
  source run, so any future GC has to be reference-aware.
- Fan-out is fail-fast *by default*. `map` and `gather` take `return_exceptions=True` for
  collect mode, which records a terminal `TaskFailed` per collected failure (ADR-0027,
  superseding ADR-0020). `start_child` has no flag; collect it as a `gather` member.
- `satay runs show` is frozen at the V1 event subset (ADR-0016). Post-V1 events render as
  bare type lines by design, and Studio covers the rest. One carve-out: `TaskFailed` is
  summarised, because it is `TaskCompleted`'s terminal twin rather than a new kind of
  durable call (ADR-0016 refinement, KAN-957). Not a precedent for the others.
- Fork accepts terminal runs only (ADR-0004).
- No PostgreSQL, no multi-worker, no distributed execution. One process, one writer.
- Async only. No sync workflows or tasks.
- Nondeterminism detection is runtime-only and compares the durable-call schedule, not
  arguments. No static analysis, no automatic cross-version workflow migration.
- Windows is best-effort (the cross-process data-dir lock is POSIX `flock` and degrades to
  a no-op elsewhere), and SQLite on network filesystems is unsupported (ADR-0019).

## Direction

`0.1.0` has shipped. Two ADRs set what comes next and what counts as important.

[ADR-0025](docs/adr/0025-positioning-agents-first.md), agents first, platform second. The
debugger is the wedge. Durability is a commodity claim in 2026 (Temporal, Restate, DBOS,
Inngest, Hatchet), so fork-from-a-prefix, replay, and call-by-call compare, locally with
no account, are what nobody else has. The first user is an app developer building AI
features, not a platform team. PostgreSQL, multi-worker and distributed execution keep
their ARCHITECTURE §9 ordering and come after the launch. The no-agent-abstraction
non-goal holds: five primitives and cookbook examples, no loop framework, no provider
adapters, no graph DSL.

[ADR-0026](docs/adr/0026-license-and-hosted-journal-plane.md), Apache-2.0 forever plus a
hosted journal plane. Nothing is withheld from a self-hosted user. The paid tier is
hosting only (journal ingest, retention, hosted Studio, team sharing, cost reporting),
never hosted execution. One requirement landed early, write-time redaction
([ADR-0029](docs/adr/0029-write-time-redaction.md)): off by default, slot-scoped to the
`*_ref` value fields so replay identity is untouched. Read-time redaction is still the
default and still protects only the API response. No hosting implementation exists.

sibei-flow is the sibling project and the designated first tenant. The two are independent
products sharing one engine, and the coupling surface is the journal read format (stdlib
frozen dataclasses), not the execution core. Its needs are legitimate input, but ADR-0025
wins where they conflict. See `docs/CONTEXT.md` § "Relationship to sibei-flow".

## Commands

Use the `make` targets. The hook and CI both go through them, so they cannot drift.
`make help` lists everything.

```bash
make dev          # uv sync (dev group)
make dev-studio   # uv sync --extra studio --frozen, what every CI job installs
make lint         # ruff check + ruff format --check
make type         # mypy --strict over src (syncs the studio extra first)
make check        # lint + type
make test         # unit tests only, the fast inner loop
make test-all     # the full suite (unit + integration + e2e), as CI runs it
make docs         # docs version check + repo-link check + zensical build --strict
make docs-version # flip every version the docs quote to the newest release tag
make secrets      # gitleaks over history + tree (needs gitleaks on PATH)
make ci           # everything CI gates on: check + test-all + docs
```

Three things that will otherwise cost you an afternoon:

- **mypy needs the studio extra.** Without `fastapi`, `uvicorn` and `typer` installed it
  reports 21 import and decorator errors in `satay.control` and `satay.devstack` that say
  nothing about the code. `make type` syncs it for you, so a bare `uv run mypy src` on a
  dev-only env will look broken when it is not.
- **A whole-suite run needs the studio extra too**, and aborts at collection without it
  (KAN-460). A narrowed run (`tests/unit`, a file, a node id, `-k`, `-m`, `--lf`) only
  warns, which keeps `make test` green on a plain `make dev` env.
  `SATAY_ALLOW_MISSING_STUDIO_EXTRA=1` downgrades the error, and never belongs in CI.
- **`.python-version` pins 3.13**, the newer of the two versions CI runs. If a failure
  looks version-shaped, check `uv run python -V` first.

Install the pre-push hook with `make install-hooks` (bypass with `git push --no-verify`).
It mirrors what branch protection requires: `make check`, `make test-all`, `make docs`,
and `make secrets` when `gitleaks` is on PATH.

## Workflow conventions

- `main` is protected. PR-only, never push to `main`.
- Branch per slice: `git switch -c feat/<slice>` off `origin/main`, then open a PR.
- Run `make ci` before pushing. The pre-push hook mirrors it.
- Commit trailer: `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`.
- **Use `treehouse` for worktrees.** It keeps a pool of pre-warmed git worktrees so
  several agents can work this repo in parallel. `treehouse get` acquires one and opens a
  subshell, `treehouse status` shows the pool, `treehouse return` gives one back, and
  `treehouse prune` clears stale entries. One agent, one worktree: two agents sharing a
  checkout will `git switch` under each other, which has already produced a near-miss on a
  staged commit here.

## Module map (mirrors ARCHITECTURE §1)

```
src/satay/
  api/         author decorators, 5 primitives, run handle, TaskContext   (A1)
  replay/      replay engine, identity resolver, nondeterminism           (A2)
  journal/     event model (frozen dataclasses), Store seam, codec        (A3)
  executor/    TaskExecutor seam, LocalTaskExecutor, retry/backoff        (A4)
  timers/      timer + event poll loop, event inbox                       (A5)
  control/     HTTP control + read API, redactor on reads; studio only    (A7/A8)
  versioning/  code-version stamper + mismatch policy                     (A10)
  blobs/       payload spill to local files over 256 KiB                  (A3.4)
  devstack/    `satay dev` orchestrator; studio extra only                (A9)
  testing/     fault injection, manual clock, seeded RNG, fixtures        (ADR-0011)
  cli/         core argparse CLI (`satay runs show`)                      (U1)
```

Public surface, re-exported from `satay/__init__.py`. `tests/unit/test_public_surface.py`
is the authority, so keep the two in step:

- **Authoring:** `workflow`, `task`, `TaskContext`, `task_context`
- **Primitives:** `start`, `sleep`, `wait_for_event`, `send_event`, `map`, `gather`,
  `start_child`
- **Entry points:** `run_app` (ADR-0030; `async with` gives you an open journal and a
  running poll loop, and it is core, not `satay[studio]`), `fork` (ADR-0028)
- **Values:** `RunHandle`, `PARKED` (ADR-0030; what `result()` returns for a run parked
  with nothing in this process to wake it, and it is not `None`)
- **Errors:** `WorkflowFailedError`, `TaskFailedError`, `NondeterminismError`,
  `EffectSafetyError`, `VersionMismatchError`

## The core-dependency boundary (do not cross)

The runtime core is pure-Python with near-zero third-party deps (ADR-0013/0016). Hold this
line, it is the product's main packaging promise:

- **No Pydantic in the core.** It is duck-typed: call `model_validate` only when a
  declared return type provides it, behind an optional import.
- **The CLI uses `argparse`.** Typer and `satay dev` live only in `satay[studio]`.
- **FastAPI and uvicorn** live only in `satay[studio]`.
- **Events are stdlib frozen dataclasses**, and persistence is raw SQL over stdlib
  `sqlite3` with no ORM.
- **Retry and backoff are hand-rolled** against the injected clock (no `tenacity`), stdlib
  `logging`, and no `dulwich` (git binary, falling back to a source hash).
- `tests/integration/test_import_hygiene.py` enforces that importing the core pulls in
  none of `fastapi`, `uvicorn`, `pydantic`, `typer` or `click`. Keep it green.

## Test seam (ADR-0011)

The primary seam is the public API driving real workflows against a temp SQLite store,
with injected determinism controls: `ManualClock`, `SeededRng`, and `FaultInjector`, which
crashes or stalls after a named journal event. Assert observable outcomes (result, status,
journal, execution-count marker), never private replay internals. Fixtures live in
`satay.testing.fixtures`, loaded as a pytest plugin from `tests/conftest.py`.

## Persistence (ADR-0017)

Data lives under a project-local `./.satay/`, overridden by `--data-dir` or
`SATAY_DATA_DIR`. The schema is versioned with `PRAGMA user_version` and migrations are
forward-only. See `satay/config.py` for the layout and `satay/journal/store.py` for the
live `SQLiteStore`, whose `SCHEMA_VERSION` is authoritative (WAL, and it refuses a DB
newer than the code).

## Pointers

- `docs/ARCHITECTURE.md` §1 to §2, structure and system model
- `docs/CONTEXT.md`, glossary and decision register D1 to D23
- `docs/RELEASING.md`, the release procedure, including the docsite version flip
- ADRs of record: 0011 (test seam), 0012 (co-hosting/single-writer), 0013 (packaging),
  0015 (toolchain), 0016 (core deps), 0017 (persistence), 0019 (platform/release)
- `docs/SLICE-V*.md`, per-slice scope. V1 is the two-task crash-recovery headline
