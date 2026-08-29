# ADR-0040 — PostgreSQL `Store` and multi-worker execution: an additional mode, not a replacement

- **Status:** Proposed
- **Date:** 2026-08-28
- **Deciders:** Jian (leejianrong2@gmail.com)

Reopens the ordering [ADR-0025](0025-positioning-agents-first.md) set ("PostgreSQL, multi-worker
and distributed execution wait for ARCHITECTURE §9's ordering, after launch") — the product owner
has explicitly chosen to build this now, ahead of that ordering; this ADR does not re-litigate
that call, only the design. Extends [ADR-0012](0012-api-cohosting-and-single-writer.md) (SQLite
single-writer via an in-process command queue) and [ADR-0007](0007-runtime-and-worker-model.md)
(single-process asyncio worker, the `TaskExecutor` seam) with a second mode behind the existing
`Store` seam, per ARCHITECTURE §9's phased roadmap (SQLite → PostgreSQL → multi-worker) and
[ADR-0039](0039-run-deletion-and-blob-gc.md)'s precedent of extending the `Store` Protocol rather
than rewriting it.

## Context

ARCHITECTURE §9 already sketches this: a `PostgresStore` behind the `Store` seam, `LISTEN/NOTIFY`
replacing the poll loop, then multi-worker via `SELECT ... FOR UPDATE SKIP LOCKED` and
leases/heartbeats. That sketch is a paragraph, not a design; re-reading it against the actual
code (`src/satay/executor/`, `src/satay/control/commands.py`, `src/satay/timers/__init__.py`)
surfaced three things worth correcting or making explicit before writing any code:

1. **"A second `TaskExecutor` implementation" is the wrong seam for multi-worker.**
   `TaskExecutor` (`src/satay/executor/__init__.py`) governs one task's retry/backoff/timeout
   inside a single drive — it has nothing to do with which *worker process* drives a run.
   Multi-worker's actual unit of coordination is `TimerEventWorker` (`src/satay/timers/__init__.py`):
   several instances of the *same* poll loop, in separate processes, all reading the same
   Postgres store. No new `TaskExecutor` is needed for multi-worker itself.
2. **ADR-0012's command queue cannot span processes.** "The API routes all writes to the worker
   through an in-process command queue" (`CommandQueue`, `src/satay/control/commands.py`) is an
   in-memory, single-process structure by construction. It works for `satay dev`'s one process;
   it cannot route a `start`/`cancel`/`send_event`/`fork` from an HTTP replica to whichever of
   several worker *processes* happens to end up driving a given run. Multi-worker needs commands
   to become durable, poll-visible rows — the same shape `timers`/`event_inbox` already use — not
   an optional nicety. Flagged prominently because ARCHITECTURE §9's sketch doesn't mention it at
   all, and skipping it would make multi-worker silently drop control-plane writes.
3. **`TimerEventWorker._deliver_events`/`_fire_timers` scan every non-terminal run every tick**
   via `list_runs()`. Under multi-worker this scan is naturally redundant across workers — every
   worker sees the same candidate set every tick. Correctness comes from the per-run lease
   (Decision 4) gating the actual re-drive, not from partitioning the scan up front. Fine at the
   scale ADR-0025 targets; a claim-aware query (`WHERE lease_expires_at < now()`) is a legitimate
   future fast path, not addressed here — the same "revisit if it bites" posture ADR-0037 already
   took with its own full-store mark-phase scan.

## Decision

**1. Postgres is an additional mode, never the default.** `SQLiteStore` and the in-process
`CommandQueue` are unchanged and remain what `satay dev`, the quickstart, and the test suite's
default fixtures use. A user opts in explicitly (`PostgresStore.open(dsn)`, or equivalently
`SATAY_STORE=postgres` + a DSN env var, mirroring `SATAY_DATA_DIR`'s override pattern) — never
inferred. Choosing Postgres does not itself require multiple workers: one worker against Postgres
is a fully supported mode on its own (durability across process restarts on shared infra), and is
delivered as its own slice before multi-worker (Decision 7).

**2. `PostgresStore` implements the existing `Store` Protocol exactly**, including ADR-0039's two
additions (`delete_run`, `referenced_blob_ids`). Built on **psycopg3**, using its **async**
connection/pool (`psycopg.AsyncConnection` via `psycopg_pool.AsyncConnectionPool`) rather than a
sync connection wrapped in `async def` the way `SQLiteStore` wraps stdlib `sqlite3` — Postgres I/O
is genuine network I/O and must not block the worker's event loop the way a local SQLite call
never really does. **Raw parameterized SQL, no ORM** — matches ADR-0016's existing rule for
`SQLiteStore` and keeps the two implementations symmetric enough to eyeball for behavioral parity.
Schema mirrors the SQLite tables (`runs`, `events`, `timers`, `event_inbox`) with Postgres types,
plus two new tables this ADR introduces (Decisions 4–5). Forward-only migrations, versioned via a
`schema_migrations` table (Postgres has no `PRAGMA user_version` analogue).

**3. Blob storage stays local-filesystem in this phase, for both modes.** `BlobStore` is
unchanged; a Postgres-backed deployment still spills to a local directory on whichever machine a
worker runs on. Multi-worker across **more than one machine** therefore needs a shared filesystem
for spilled blobs (or spill simply doesn't work cross-machine) — a named limitation, not solved
here. A `BlobStore` seam swap to object storage is real future work but orthogonal: multi-worker's
correctness does not depend on it, and single-machine multi-worker (several processes, one disk)
is unaffected.

**4. Multi-worker correctness rests on a run-level lease, not a global single writer.** A new
`run_leases` table (`run_id PK, worker_id, lease_expires_at`) — not columns bolted onto `runs` —
so `RunRecord` and the SQLite schema stay untouched; leasing is meaningless for SQLite's
single-writer model and has no business in the shared dataclass.

- Before re-driving a run (a fresh `start`, a fired timer, a delivered event, an applied
  command), a worker attempts an atomic claim — conceptually
  `INSERT ... ON CONFLICT (run_id) DO UPDATE ... WHERE run_leases.lease_expires_at < now()
  RETURNING worker_id`, succeeding only when no lease exists or the existing one has expired. A
  worker that doesn't get its own id back skips that run this tick — the same "skip, don't
  block" posture `_deliver_events` already has for a wait with no matching event yet.
- The worker **heartbeats** (renews `lease_expires_at`) partway through a long replay so a slow
  but alive worker doesn't lose its own lease mid-drive, and **releases** the lease when the
  drive parks or terminates rather than waiting out the full TTL.
- Lease duration and heartbeat interval are configurable, defaulting to comfortably larger than
  one poll interval (e.g. a 30s lease, heartbeat every 10s) so ordinary scheduling jitter never
  costs a healthy worker its own lease.
- **Defense in depth, not the only guard:** `events`' existing `PRIMARY KEY (run_id, seq)`
  already rejects a duplicate `seq` insert outright. If a lease bug ever let two workers drive
  one run concurrently, one of the two transactions fails on that constraint instead of
  corrupting the journal — the lease makes the case rare-to-never, the primary key makes it
  non-catastrophic if it ever happens anyway.
- Implemented as a claim-then-release wrapper around `TimerEventWorker`'s existing `_redrive`
  call sites (`_deliver_events`, `_fire_timers`, `_apply_commands`); the poll loop's own
  scan-then-decide structure is unchanged.

**5. Commands become a durable table under Postgres/multi-worker, not an in-process queue**
(Context, point 2). A new `commands` table (`command_id PK, run_id, kind, payload, created_at,
applied_at NULL`) replaces `CommandQueue` for this mode: the control API — any replica — inserts a
row, and **any** worker's poll tick drains unapplied rows and applies them through the same
`apply_command` function `_apply_commands` already calls. Behind a small seam (a `CommandSink`
protocol: `enqueue` / `drain`) so `CommandQueue` itself is untouched and stays what the
single-process SQLite mode uses — a second implementation, not a rewrite of the first.

**6. `LISTEN/NOTIFY` is explicitly deferred to a follow-up.** This phase reuses the existing ~1s
poll loop unchanged, against Postgres instead of SQLite. Push-based wake is real future work once
polling-on-Postgres is proven — the same incremental posture ADR-0036 took for read performance.

**7. Delivered in two implementation slices, in order:**

- **(a) `PostgresStore` alone.** Single worker, no leasing, no durable command table — a
  Postgres-backed deployment behaves exactly like today's SQLite deployment except state lives in
  Postgres. Verified by running a Postgres-parametrized subset of the existing store/integration
  suite against both backends, proving `Store` seam parity before any multi-worker complexity is
  added.
- **(b) Multi-worker.** `run_leases`, the durable `commands` table, and the claim-then-release
  wrapper. Depends on (a) merged and green.

## Consequences

- SQLite stays the zero-infrastructure default; nothing about the existing local-first path
  changes.
- A new third-party dependency, `psycopg` (and `psycopg_pool`), enters an **optional extra**
  (e.g. `satay[postgres]`) — never the core dependency set, mirroring how `satay[studio]` isolates
  FastAPI/uvicorn/Typer (ADR-0013/0016). `tests/integration/test_import_hygiene.py`'s core-import
  guarantee is unaffected; a Postgres-specific import-hygiene test is added alongside it.
- Two tables specific to the Postgres backend (`run_leases`, `commands`) with no SQLite
  equivalent — the `Store` Protocol's public method surface stays identical across both backends;
  these are `PostgresStore` implementation details, not part of the seam.
- Cross-machine multi-worker requires a shared filesystem for blobs until an object-storage-backed
  `BlobStore` exists (not scoped here).
- `docs/ARCHITECTURE.md` §9's phased roadmap gets a correction: multi-worker's coordination unit
  is the poll loop via a lease, not a second `TaskExecutor` implementation.

## Alternatives considered

- **`SELECT ... FOR UPDATE SKIP LOCKED` per due timer/event, instead of a run-level lease** —
  rejected: still needs a run-level exclusion underneath during the actual replay+append (two
  workers must never drive one run concurrently), so it adds a second locking layer for the same
  safety property a single lease already provides.
- **asyncpg instead of psycopg3** — rejected: psycopg3 supports both sync and async and sits
  closer in spirit to stdlib `sqlite3`; asyncpg's main edge, more natural `LISTEN/NOTIFY`
  ergonomics, doesn't matter yet since this phase stays on polling (Decision 6).
- **Extend `RunRecord`/the `runs` table with lease columns shared across both stores** —
  rejected: leasing is meaningless for SQLite's single-writer model, and unused columns on a
  shared dataclass for one backend's benefit is exactly the coupling the `Store` seam exists to
  avoid.
- **Build `PostgresStore` and multi-worker in one slice** — rejected: (a) alone is independently
  valuable (durability across restarts, no code changes for a caller already using the `Store`
  seam correctly) and independently testable; shipping it first proves seam parity before leasing
  and the durable command table add real complexity on top.
