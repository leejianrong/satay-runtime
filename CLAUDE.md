# CLAUDE.md — agent brief for Satay Runtime

Satay is a **local-first durable-execution runtime** for async Python: `async def`
workflows/tasks whose durable calls are recorded to an append-only journal and
**replayed from the top** on crash, reusing recorded results. One process, SQLite,
no external infra. The debugger (Studio) ships in the optional `satay[studio]` extra.

## Build status — trust the code over the docs

**V1–V8 are merged; the MVP is built and the full suite is green.** Nothing in `src/`
raises `NotImplementedError` any more. The `docs/` describe the *intended* full system;
where docs and code disagree, the code is the truth for what exists today — verify before
you believe a docstring, including this section.

Deliberately no test count here: it goes stale on the next PR that adds one. Run
`uv run pytest -q` for the real number, and treat a red suite (not a changed count) as the
signal that something is wrong.

What is real now:

- **Durable core + replay** (`replay/`): replay from the top over an append-only journal,
  reusing recorded results; identity resolution and nondeterminism detection under a
  `nondeterminism` policy (strict/warn/off) that is **strict by default** — a divergent
  replay raises `NondeterminismError` (ADR-0022). It is a separate knob from
  `effect_safety`; do not conflate them.
- **SQLite journal** (`journal/`): `SQLiteStore` on raw stdlib `sqlite3`, versioned by
  `PRAGMA user_version` with forward-only migrations, WAL, per-run async writer lock.
- **Execution guarantees** (`executor/`): retries with capped exponential backoff and
  full-jitter delays off the injected clock and RNG, at-least-once execution,
  runtime-derived idempotency keys, and `effect_safety` (strict/warn/off, **warn by
  default**) guarding retryable `side_effect=True` tasks that are not declared
  `idempotent=True`. It governs *only* that check — replay divergence is the separate
  `nondeterminism` policy (ADR-0022).
- **Time and events** (`timers/`): durable `sleep`, `wait_for_event`/`send_event` over a
  persistent inbox, and the timer + event poll loop (FIFO, event-wins-over-timeout).
  `async with satay.run_app() as store:` (`api/app.py`) is the supported way to get that
  loop without `satay dev` — **core, no studio extra** (ADR-0030). Inside it a parked
  run's `result()` waits for the wake; outside it, `result()` returns `satay.PARKED`,
  never `None`.
- **Composition** (`api/primitives.py`): `map`/`gather`/`start_child` as keyed durable
  calls, with partial-completion recovery mid-fan-out.
- **Control plane** (`control/`): HTTP control + read API, a `Redactor` forced on every
  read, and a loopback/token `SecurityPolicy`; writes serialize through a `CommandQueue`.
- **Studio** (`_studio_assets/`): the built SPA bundle, served at `/` by the V5 process.
- **Fork, compare, versioning** (`control/commands.py`, `versioning/`): prefix fork, run
  comparison by durable-call identity, code-version stamp + mismatch policy on resume.
- **`satay dev`** (`devstack/`): lock → store → worker → server, torn down in reverse.
- **Payload spill** (`blobs/`): payloads larger than `SPILL_THRESHOLD_BYTES` (256 KiB) go
  to content-addressed blob files, transparent on write and read.
- **Test seam** (`testing/`): `ManualClock`, `SeededRng`, `FaultInjector`, pytest fixtures.

Deliberate MVP gaps — do not "fix" these without a card:

- **No blob GC**, no run deletion, no compaction (ADR-0004). Forks share blobs with their
  source run, so any future GC must be reference-aware.
- **Fan-out is fail-fast by *default*** — `map`/`gather` take `return_exceptions=True` for
  collect mode, which records a terminal `TaskFailed` per collected failure (ADR-0027,
  superseding ADR-0020). `start_child` has no flag; collect it as a `gather` member.
- **`satay runs show` is frozen at the V1 event subset** (ADR-0016); Studio covers the
  rest. Post-V1 events render as bare type lines by design.
- **Fork accepts terminal runs only** (ADR-0004).
- **No PostgreSQL, no multi-worker, no distributed execution.** One process, one writer.
- **Async only** — no sync workflows/tasks.
- **Nondeterminism detection is runtime-only** and compares the durable-call schedule, not
  arguments; no static analysis, no automatic cross-version workflow migration.
- **Windows is best-effort** (the cross-process data-dir lock is POSIX `flock` and
  degrades to a no-op elsewhere); SQLite on network filesystems is unsupported (ADR-0019).

## Direction (decided 2026-08-05 — read before proposing roadmap work)

Two ADRs set what Satay is aiming at, and they change what counts as important:

- **[ADR-0025](docs/adr/0025-positioning-agents-first.md) — the debugger is the
  wedge; agents first, platform second.** Durability is a commodity claim in 2026
  (Temporal, Restate, DBOS, Inngest, Hatchet); fork-from-a-prefix, replay and
  call-by-call compare, locally with no account, are what nobody else has. **The
  first user is an app developer building AI features, not a platform team.** So:
  the API-shape and usability cards (KAN-476/477/481/491/520/524/579 and kin) are
  **launch blockers, not cleanup**, and collect-mode fan-out (KAN-473, reopening
  ADR-0020) is **on the critical path** because "draft N candidates, keep the best"
  is the shape of agentic work. PostgreSQL, multi-worker and distributed execution
  keep their ARCHITECTURE §9 ordering but come **after** the launch.
  The **no-agent-abstraction non-goal holds**: five primitives and cookbook
  examples, no loop framework, no provider adapters, no graph DSL. The
  **vendor-dossier reference app is cut**; sibei-flow's repair worker is the
  reference consumer instead.
- **[ADR-0026](docs/adr/0026-license-and-hosted-journal-plane.md) — Apache-2.0
  forever plus a hosted journal plane.** Nothing is withheld from a self-hosted
  user. The paid tier is tier-1 hosting only (journal ingest, retention, hosted
  Studio, team sharing, cost reporting), **after** the `0.1.0` launch, never hosted
  execution. One requirement landed early: **write-time redaction**, built in
  [ADR-0029](docs/adr/0029-write-time-redaction.md) — `SATAY_WRITE_REDACTION=on` /
  `SQLiteStore.open(..., write_redaction="on")`, **off by default**, slot-scoped to
  the `*_ref` value fields so replay identity is untouched. Read-time redaction is
  still the default and still protects only the API response. The seam only: there
  is no hosting implementation and none is wanted before `0.1.0`.

**sibei-flow** is the sibling project and the designated first tenant. The two are
independent products sharing one engine; the coupling surface is the **journal read
format** (stdlib frozen dataclasses), not the execution core. Its needs are
legitimate input, but when they conflict with the app-developer roadmap, ADR-0025
wins. See `docs/CONTEXT.md` § "Relationship to sibei-flow".

## Commands

Prefer the `make` targets — the hook and CI both go through them, so they cannot
drift. `make help` lists everything.

```bash
make dev          # uv sync (dev group)
make dev-studio   # uv sync --extra studio --frozen  ← what every CI job installs
make lint         # ruff check + ruff format --check
make type         # mypy --strict over src (syncs the studio extra first)
make check        # lint + type
make test         # unit tests only — the fast inner loop
make test-all     # the FULL suite (unit + integration + e2e), as CI runs it
make docs         # check_repo_links.py + zensical build --strict
make secrets      # gitleaks over history + tree (needs gitleaks on PATH)
make ci           # everything CI gates on: check + test-all + docs
```

The raw commands, if you need one in isolation:

```bash
uv sync                        # env + deps (installs the dev group)
uv run ruff check .            # lint
uv run ruff format .           # format (add --check in CI/hooks)
uv sync --extra studio --frozen && uv run mypy src   # type-check, strict
uv run pytest tests/unit -q    # unit tests
uv run pytest tests/integration --collect-only -q   # import-hygiene guard
uv sync --extra studio && uv run pytest -q          # full suite
python3 docsite/check_repo_links.py                 # links from the site into the repo
cd docsite && uvx --from "zensical==0.0.52" zensical build --strict --clean
```

**`mypy` needs the studio extra.** Without `fastapi`/`uvicorn`/`typer` installed it
reports 21 `import-not-found` and `untyped-decorator` errors in `satay.control` and
`satay.devstack` that say nothing about the code. `make type` syncs it for you; a
bare `uv run mypy src` on a dev-only env will look broken when it is not.

**`.python-version` pins Python 3.13** — the newer of the two versions CI runs (3.12 and
3.13) — so `uv sync` builds every checkout *and every agent worktree* on the same
interpreter CI uses. If a failure looks version-shaped, check `uv run python -V` first.

**The full suite needs the `studio` extra, and now says so.** Without it the
FastAPI/Studio modules `importorskip` themselves away, so the run used to report a
green, smaller count with a whole tier missing. Since KAN-460 a **whole-suite** run
(`uv run pytest -q`, `make test-all`, CI) aborts at collection with a usage error
naming the fix; a **narrowed** run (`tests/unit`, a file, a node id, `-k`, `-m`,
`--lf`) only warns, so `make test` on a plain `make dev` environment stays green.
Set `SATAY_ALLOW_MISSING_STUDIO_EXTRA=1` to downgrade the error to that warning —
never in CI. The gate is `tests/_extra_guard.py` + the one hook in `tests/conftest.py`;
it deliberately mirrors KAN-408's `SATAY_ALLOW_MISSING_STUDIO_BUNDLE`.

**Install the pre-push hook: `make install-hooks`** (bypass with `git push --no-verify`).
It runs `make check`, `make test-all` and `make docs`, plus `make secrets` when
`gitleaks` happens to be on PATH — roughly 55s on a warm cache. That set is chosen to
mirror what branch protection requires, including the two things the old hook missed:
mypy needs the studio extra, and CI's job *named* "Unit tests" installs that extra and
runs the **whole** suite (427 tests, not the 192 in `tests/unit`).

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

Public surface (re-exported from `satay/__init__.py`) — `tests/unit/test_public_surface.py`
is the authority, keep the two in step:

- **Authoring:** `workflow`, `task`, `TaskContext`, `task_context`
- **Primitives:** `start`, `sleep`, `wait_for_event`, `send_event`, `map`, `gather`,
  `start_child`
- **Entry points:** `run_app` (ADR-0030 — `async with`: journal open, poll loop running,
  **core, not `satay[studio]`**), `fork` (ADR-0028)
- **Values:** `RunHandle`, `PARKED` (ADR-0030 — what `result()` returns for a run parked
  with nothing in this process to wake it; **not `None`**)
- **Errors:** `WorkflowFailedError`, `TaskFailedError`, `NondeterminismError`,
  `EffectSafetyError`, `VersionMismatchError`

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
live `SQLiteStore`, whose `SCHEMA_VERSION` is the authoritative current version (WAL; it
refuses a DB newer than the code).

## Pointers

- `docs/ARCHITECTURE.md` §1–§2 — structure + system model
- `docs/CONTEXT.md` — glossary + decision register D1–D23
- ADRs of record: 0011 (test seam), 0012 (co-hosting/single-writer), 0013 (packaging),
  0015 (toolchain), 0016 (core deps), 0017 (persistence), 0019 (platform/release)
- `docs/SLICE-V*.md` — per-slice scope; V1 is the two-task crash-recovery headline
