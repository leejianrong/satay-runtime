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

## Refinement (collect-mode revisit, 2026-08-19)

**`TaskFailed` is inside the frozen subset, not outside it (KAN-957).** ADR-0027 added
`EventType.TASK_FAILED` and recorded, under this freeze, that `satay runs show` would render
it as a bare type line — "expected, not a gap". With collect mode shipped and exercised by
`examples/best_of_n_demo.py`, that reading does not hold, for two reasons.

The freeze's own wording names what it excludes: "timers and events in V3, map/child in V4,
`RunForked` in V7". Every one of those is a **new kind of durable call** the V1 renderer never
modelled. `TaskFailed` is not; it is the failure-side twin of `TaskCompleted`, and
`_summarise_payload` already summarises all four V1 task events with `task=` plus `key` /
`ordinal`. Leaving one terminal event bare inside a family the renderer otherwise covers is a
hole, not restraint — in the demo's timeline the verdict on a failed candidate rendered as
`TaskFailed` with nothing else, two lines below an attempt line naming the task, the key, the
attempt and the error, so nothing said which of two failing items it belonged to.

And the freeze's justification — "Studio (V6+) is the full-fidelity timeline" — is a division
of labour that has no other side here. `TASK_FAILED` appears nowhere in `satay/control/`, whose
`_TASK_EVENTS` whitelist is the same four V1 types (KAN-867 is the open card). A trade of CLI
restraint against Studio coverage cannot be honoured when neither renderer covers the event.

So `render_timeline` summarises `TaskFailed` with its call identity and `error=<type>:
<message>`, and no `attempt` — the verdict is on the logical call, not on one try, and the
preceding `TaskAttemptFailed` already says which attempt spent the last of the budget. The
traceback stays off the timeline, unlike `WorkflowFailed`'s: a run has one of those and can
have many of these.

**The rest of the freeze stands.** Timers, event waits, cancellation and `RunForked` still
render as bare type lines, and the broader question in KAN-445 item 2 — whether the shared
`render_timeline` should summarise the V3+ events at all, given that the examples consume it
too — is still open. This refinement is not a precedent for widening; it closes one hole in an
already-covered family.
