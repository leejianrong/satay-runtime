# CLAUDE.md — agent brief for Satay Runtime

Satay is a **local-first durable-execution runtime** for async Python: `async def`
workflows/tasks whose durable calls are recorded to an append-only journal and
**replayed from the top** on crash, reusing recorded results. One process, SQLite,
no external infra. The debugger (Studio) ships in the optional `satay[studio]` extra.

## Build status — trust the code over the docs

**Early scaffold (Epic 0).** The public surface is declared and typed, but most
behaviour raises `NotImplementedError("... lands in Vn")`. The `docs/` describe the
*intended* full system; where docs and code disagree, the code is the truth for what
exists today. What is real now:

- The full package skeleton imports cleanly and passes `mypy --strict`.
- The `testing/` module is real and unit-tested: `ManualClock`, `SeededRng`,
  `FaultInjector`, and pytest fixtures.
- Everything else is a typed stub marked with the slice it lands in.

## Commands

```bash
uv sync                        # env + deps (installs the dev group)
uv run ruff check .            # lint
uv run ruff format .           # format (add --check in CI/hooks)
uv run mypy src                # type-check, strict
uv run pytest tests/unit -q    # unit tests
uv run pytest tests/integration --collect-only -q   # import-hygiene guard
```

Shortcuts: `make check` (ruff + mypy), `make test` (unit), `make ci` (all).
Install the pre-push hook with `make install-hooks`; bypass with `git push --no-verify`.

## Workflow conventions

- **`main` is protected — PR-only, never push to `main`.**
- **Branch per slice:** `git switch -c feat/<slice>` off `origin/main`; open a PR.
- Run the cheap gates locally (`make ci`) before pushing; the pre-push hook mirrors
  them.
- Commit trailer: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

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
migrations. See `satay/config.py`. No DB exists yet (lands in V1).

## Pointers

- `docs/ARCHITECTURE.md` §1–§2 — structure + system model
- `docs/CONTEXT.md` — glossary + decision register D1–D22
- ADRs of record: 0011 (test seam), 0012 (co-hosting/single-writer), 0013 (packaging),
  0015 (toolchain), 0016 (core deps), 0017 (persistence), 0019 (platform/release)
- `docs/SLICE-V*.md` — per-slice scope; V1 is the two-task crash-recovery headline
