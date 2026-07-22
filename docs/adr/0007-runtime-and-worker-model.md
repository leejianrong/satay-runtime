# ADR-0007 — Local-first single-process asyncio runtime

- **Status:** Accepted
- **Date:** 2026-07-20
- **Deciders:** Jian (leejianrong2@gmail.com)

## Context

The MVP must prove the durable-execution property without prematurely building a
distributed system. Distributed multi-worker execution is an explicit non-goal.
The programming-model examples are uniformly `async def`.

## Decision

- **Async-only** workflows and tasks for the MVP. Sync work is the user's
  responsibility to wrap in a thread; native sync support is deferred.
- **Single-process asyncio worker.** `satay.map(concurrency=N)` and
  `satay.gather` express asyncio concurrency within that one process.
- Task execution passes through a **`TaskExecutor` interface from day one**;
  the only MVP implementation is `LocalTaskExecutor`. A future
  `PostgresTaskExecutor` / multi-worker model can be added behind the same seam.
- `satay dev` runs one process containing: the asyncio worker, the SQLite store,
  the HTTP control API, and Satay Studio (see ADR-0009).
- Durable sleep and event-wait timeouts are timer rows the worker polls
  (~1s in dev); no external scheduler.

## Consequences

- Keeps the MVP small while preserving a clean worker boundary for later
  distribution.
- Concurrency is bounded by one process/event loop; acceptable for local-first.
- The executor seam must be respected even though only one impl exists, so the
  boundary does not rot.
