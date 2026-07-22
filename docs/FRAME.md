---
shaping: true
---

# Satay Runtime — Frame

## Source

Verbatim from the project's planning material (`REQS.md`, `initial_planning_summary.md`):

> **A transparent, durable Python runtime for AI-enabled applications and workflows.**
> The product should make ordinary Python application code durable, inspectable,
> resumable, and replayable without forcing developers into a heavy
> framework-specific programming model.

> Write ordinary async Python. Satay records every step, survives failures, and
> shows you exactly what happened.

> A practical first end-to-end milestone: Run a workflow containing two tasks,
> persist every transition to SQLite, kill the process after the first task
> completes, restart it, reuse the first task result, execute the second task,
> and show the complete execution timeline locally.

---

## Problem

Python engineers building AI-enabled applications and pipelines need durability
(survive crashes, resume without redoing completed work) and transparency (see
exactly what happened when something fails). Their current options are both bad:

- **Hand-roll it** — bespoke checkpointing, retries, and idempotency scattered
  through the app, with no consistent history and painful post-failure debugging.
- **Adopt a heavyweight orchestration framework** — which forces ordinary code
  into graph DSLs and framework message/state classes, hides native stack traces,
  causes ecosystem lock-in, and often needs a hosted paid service to inspect runs.

They want reliability without rewriting their application around a framework, and
without giving up native async Python, native errors, or local debugging.

## Outcome

A developer writes ordinary async Python — `@workflow` functions composing
`@task` calls with normal control flow — and gets, for free:

- **Durable execution:** every step recorded to an append-only journal; a crashed
  run resumes by replaying and reusing already-completed task results.
- **Local transparency:** Satay Studio (launched by `satay dev`) shows the
  timeline, execution tree, logical-vs-physical attempts, native stack traces,
  inputs/outputs, retries, and model/token/cost — no hosted service.
- **No lock-in:** ordinary values in and out, native exceptions, the developer's
  own provider SDK calls inside tasks; no mandatory framework classes or graph DSL.

Success for the MVP is the two-task crash-recovery slice working end to end on
SQLite, with the interruption and resume visible in the timeline — proving the
core durable property before any showcase app or ecosystem is built.
