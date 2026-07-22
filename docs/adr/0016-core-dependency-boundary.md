# ADR-0016 — Core dependency boundary and data representation

- **Status:** Accepted
- **Date:** 2026-07-21
- **Deciders:** Jian (leejianrong2@gmail.com)

## Context

ADR-0013 committed to a lean, near-stdlib core but left three implementation choices
open that together decide whether the core actually stays lean: where the CLI lives
(Typer pulls click and rich), how journal events are represented now that Pydantic is
out of the core, and whether database access goes through an ORM.

## Decision

- **CLI split.** The core ships a **minimal, stdlib-only CLI** for `satay runs show`
  (read-only text), built on `argparse`. **Typer and the `satay dev` command live in
  the `satay[studio]` extra.** Invoking `satay dev` without the extra fails with a
  clear message that names the install to run.
- **Event model and codec.** The journal event types and the codec use **stdlib
  frozen dataclasses** in the core. Validation stays light because the worker is the
  sole producer of events. No Pydantic in the core; rehydration of user return types
  remains duck-typed (ADR-0013). `msgspec` may be adopted later, behind the codec
  seam, only if encode/decode throughput demands it.
- **Database access.** **Raw parameterized SQL over stdlib `sqlite3`.** No ORM and no
  SQLAlchemy, consistent with the dedicated-writer-thread control in ADR-0012.

## Consequences

- The core keeps a near-stdlib dependency footprint; the heavier CLI stack is opt-in
  with the extra.
- Hand-written SQL and dataclasses mean more explicit code, in exchange for full
  control and no heavy dependencies.
- Refines ADR-0013; relies on ADR-0012.

## Refinement (H3 test audit, 2026-07-22)

- **`satay runs show` is frozen at the V1 event subset for the MVP (Q50).** The core CLI
  renders the V1 core events and is not extended to the event types added later (timers
  and events in V3, map/child in V4, `RunForked` in V7). Studio (V6+) is the full-fidelity
  timeline; the CLI stays a bootstrap inspector for the durable core. The freeze is
  documented so the missing CLI coverage of later event types is deliberate, not a gap.
