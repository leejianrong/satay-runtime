# ADR-0009 — Local surfaces: Studio web app, control API, event polling

- **Status:** Accepted
- **Date:** 2026-07-20
- **Deciders:** Jian (leejianrong2@gmail.com)

## Context

The local debugger is a core product surface — the immediate reason to pick
Satay. `satay dev` launches it. Form-factor options: (A) local web app, (B)
terminal UI, (C) both. Separately, an outside caller must be able to `start`,
`status`, `cancel`, and `send_event` to a running workflow, which an
in-process-only API cannot serve.

The selling views — timeline, execution tree, run comparison, redaction — need
real screen space. A TUI is lighter but cramped for these and undersells the
debugging wedge. Building both now splits scarce MVP effort.

## Decision

- **Satay Studio is a local web app** served on localhost by `satay dev`, built
  over a **JSON API**.
- The same process exposes a **local HTTP control API** (`start` / `status` /
  `cancel` / `send_event`) that writes to the store; the **worker picks up events
  and due timers by polling** the store.
- Studio (read) and the control API (write) share one JSON API seam.
- A TUI is explicitly deferred; the API seam keeps it cheap to add later.

## Consequences

- Adds a bundled frontend build and a served process to `satay dev`.
- One well-defined JSON API serves both the debugger and external callers.
- Event delivery latency is bounded by the poll interval (~1s in dev), acceptable
  local-first; a push mechanism can come later without changing the API contract.

## Refinement (H3 test audit, 2026-07-22)

- **Interruption marker has one definition and one owner (Q42, corrected by Q52).** The
  timeline's ⚡ interruption/resume marker is computed **once in the read/view layer** and
  consumed by both the `satay runs show` CLI and Studio, so the two cannot disagree. Q42
  first defined it as a `WorkflowWaiting` → `WorkflowResumed` transition; **Q52 corrected
  this** — see the H4 refinement below — because a crash writes no `WorkflowWaiting`, so
  that wording would render no marker on the headline V1 crash.
- **Compare endpoint belongs to the read API (Q44).** `GET /runs/{id}/compare` is a pure
  read over the journal and is owned, implemented, and tested with the other read
  endpoints in V5. V7 adds only the Studio side-by-side **view** on top, mirroring how
  the `fork` route is a V5 stub with its semantics in V7.

## Refinement (H4 slice application, 2026-07-22)

- **Interruption marker = presence of a `WorkflowResumed` event (Q52).** Applying the Q42
  wording to the slice test plans exposed a defect: the V1 crash-recovery demo produces a
  `WorkflowResumed` with **no preceding `WorkflowWaiting`** (a hard kill cannot append one,
  and `WorkflowWaiting` is only introduced in V3), so the strict transition rule would
  render **no** ⚡ on the one case the marker exists for. The resolution moves the
  distinction into the writer, which already knows it: **the worker appends
  `WorkflowResumed` only when re-driving a run that was *not* durably parked** — i.e. one
  interrupted mid-execution. A graceful wake from a `WorkflowWaiting` (a `sleep` or an
  awaited event) writes no `WorkflowResumed` and carries no ⚡, because nothing was lost.
  The read/view-layer marker is then simply the **presence of a `WorkflowResumed` event** —
  trivially testable and semantically crisp ("recovered from an interruption").
- **A crash *while parked* correctly shows no marker.** A run that had already written
  `WorkflowWaiting` and released the process loses nothing if the process then dies; its
  later wake is indistinguishable from a normal one and is treated as such. This is the
  intended, honest outcome, not a gap.
- **The rejected alternative** was to synthesize a `WorkflowWaiting` on crash-restart so the
  transition matched; that writes an event which never happened into an append-only journal
  whose whole purpose is to record exactly what happened (ADR-0004), and was declined.
- **Scope:** for the MVP the ⚡ means **crash interruption only**; healthy durable waits are
  shown by their inline `WorkflowWaiting`/`TimerFired`/`ExternalEventReceived` events. A
  separate "resumed from wait" indicator is optional later polish.
