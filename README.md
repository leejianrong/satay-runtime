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

> **Status: early scaffold (Epic 0).** The public surface is declared and typed, but
> most behaviour raises `NotImplementedError("... lands in Vn")`. Trust the code over
> the docs. See `docs/` for the specs and `CLAUDE.md` for the build brief.

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
