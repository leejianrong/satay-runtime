# ADR-0012 — API co-hosting, single-writer model, and SQLite driver

- **Status:** Accepted
- **Date:** 2026-07-21
- **Deciders:** Jian (leejianrong2@gmail.com)

## Context

The first-pass architecture co-hosted the HTTP control/read API on the worker's
own asyncio loop and, at the same time, described the journal as written by a
**single writer under an async lock**. Those two claims collide. §2 also has the
control API writing to the store (`start`/`cancel`/`send_event`/`fork`), which makes
it a **second writer**, and SQLite (even in WAL mode) permits only one writer at a
time. Co-hosting has a second cost: the worker loop also runs user task coroutines,
so any accidental blocking (a sync SDK call, a large decode, CPU work) freezes the
whole loop, and with it the debugger, exactly when a run is busy.

Options weighed:
- **(A) In-loop.** One loop and one connection, so the single writer is literally
  true, but a blocking task stalls Studio.
- **(B) Separate-thread API, writing directly.** Responsive, but a genuine second
  writer needing `BEGIN IMMEDIATE` + `busy_timeout` + `SQLITE_BUSY` retries.
- **(C) Separate-thread API, reads direct, writes via a command queue to the worker.**
  Responsive and still one writer.

Going to **PostgreSQL from day one** was also considered (it removes SQLite's
single-writer mechanics and offers `LISTEN/NOTIFY`), and declined for the MVP: it
would require a running Postgres just to see the two-task demo, eroding the
local-first, zero-infrastructure wedge, and it does not answer the underlying design
question. See the phased roadmap in ARCHITECTURE §9.

## Decision

- The **control + read API runs on its own thread** with its own event loop, not on
  the worker loop. The FastAPI + uvicorn server runs inside that thread (the server
  stack itself lives in the `satay[studio]` extra, ADR-0013).
- The API **reads SQLite directly** through read-only connections (WAL allows
  concurrent readers during a write).
- The API **routes all writes to the worker through an in-process command queue**;
  the **worker remains the sole writer**. The "single writer" property is therefore
  literally true, with no cross-writer contention.
- SQLite is driven by a **dedicated writer thread over stdlib `sqlite3`** (leaning
  choice, to be benchmarked against `aiosqlite` before the final lock), which holds
  the writer lock and allocates `seq` per run inside the append transaction.
- Pragmas set deliberately: `journal_mode=WAL`, `synchronous=NORMAL` on the append
  path, and a real `busy_timeout`.
- **SQLite is retained for the MVP**; PostgreSQL is deferred behind the existing
  `Store` seam (ADR-0007, ARCHITECTURE §9).
- Store-wide write serialization is accepted for the MVP. It is inherent to SQLite's
  single-writer model and fine at local-first scale.

## Consequences

- The debugger stays responsive under worker load; reads never block behind a busy
  worker.
- No `SQLITE_BUSY` handling, because there is exactly one writer.
- A control action (cancel/`send_event`) may take effect a little late while the
  worker is busy, bounded by the same order as the timer poll interval; acceptable
  local-first.
- The pattern is backend-portable: the command-queue single-writer carries to
  PostgreSQL unchanged, and Postgres can later relax it and add `LISTEN/NOTIFY`
  without changing the API contract.
- Supersedes the in-loop uvicorn assumption in the first-pass ARCHITECTURE §2/§3.3/
  §3.6, which are updated to match.
