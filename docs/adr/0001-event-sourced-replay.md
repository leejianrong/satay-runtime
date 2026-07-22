# ADR-0001 — Event-sourced replay execution model

- **Status:** Accepted
- **Date:** 2026-07-20
- **Deciders:** Jian (leejianrong2@gmail.com)

## Context

A durable runtime must reconstruct a workflow's position after a worker crash.
Two mechanisms exist: (A) **event-sourced replay** — re-run the workflow function
top-to-bottom, intercepting each durable call so that a call already recorded in
the journal returns its stored result instead of executing; (B) **coroutine
snapshotting** — freeze the live stack, locals, and await point and persist them,
then restore.

Option B cannot serialize a live Python stack without pickle (or a custom VM),
producing opaque, non-inspectable, module-path-coupled state that breaks on any
code change. Those consequences directly contradict Satay's stated principles:
append-only history, JSON-compatible durable data, no implicit pickle, and a
journal that doubles as the debugger.

## Decision

Use **event-sourced replay**. On resume, the workflow function re-executes
logically; each durable call (task, `map`/`gather`, `sleep`, `wait_for_event`,
`start_child`) checks the journal first: on a hit it returns the recorded result
without side effects; on a miss it executes and appends the result. The journal
is the only durable workflow state.

## Consequences

- Workflow bodies must be **deterministic** — the constraint addressed by
  ADR-0002 (identity) and ADR-0003 (nondeterminism detection).
- Orchestration logic re-runs on each recovery, but this is cheap: no I/O, only
  journal reads. All expensive work is in tasks and is reused.
- The append-only journal serves both recovery and the Studio timeline (ADR-0004).
- Forking is natural: replay from an earlier journal point (ADR-0004).
- Task code must tolerate at-least-once physical execution (ADR-0006).
