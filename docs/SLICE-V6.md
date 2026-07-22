---
shaping: true
slice: V6
---

# Satay Runtime — SLICE V6: Satay Studio web app

Everything before this proved durability through the CLI text timeline and the
JSON API. V6 is where the debugging story becomes visual: a local web app that
renders a run's timeline, its execution tree, and the detail of any task down to
its individual attempts and stack traces. It is a pure consumer of the V5 read
API, so it adds no new runtime behavior. The views that depend on fork and compare
are held back to V7. Affordance IDs reference `BREADBOARD.md`; the decision of
record is ADR-0009.

---

## Affordances

| ID | Affordance | Scope in V6 |
|----|------------|-------------|
| U2 | Run list: id, status, code version, start time | Full |
| U3 | Timeline view of ordered journal events, including the interruption and resume marker | Full |
| U4 | Execution-tree view: parent/child, child workflows, map items | Full |
| U5 | Task detail: logical task versus physical attempts, inputs and outputs, native stack trace, retry reason and delay, duration, model/token/cost | Full |

Studio reads N16 and shows redacted data from N18. It does not add or change
runtime behavior. Fork (U6), compare (U7), and the version-mismatch banner (U8)
are V7.

---

## A note on how Studio is served

The V5 process already runs the worker, the SQLite store, and the control and read
API together. V6 serves the bundled Studio frontend from that same process, so
opening Studio means pointing a browser at the running server. The single
`satay dev` command that boots the whole stack in one line is V8; until then
Studio runs against the V5 server. This is the correction rippled back into
`SLICES.md`, whose V6 note previously implied `satay dev` existed already.

---

## Build Plan

1. **Frontend build and serving.** Set up the web frontend and have the V5 server
   serve its built assets on localhost alongside the JSON API. The framework and
   build tooling are chosen during the architecture step (G); the design here only
   assumes a single-page app talking to the read API. Studio is verified through
   that API in the MVP, not through UI-rendering tests (ADR-0011).

2. **Run list (U2).** Render `GET /runs`: id, status, code version, and start
   time, ordered most-recent-first, each row linking to its run.

3. **Timeline (U3).** Render `GET /runs/{id}/timeline` as an ordered list of
   events. Draw the interruption and resume marker wherever a `WorkflowResumed`
   event appears (written only on recovery from an interruption, so its presence is
   the marker; ADR-0009/Q52), so the V1 crash is visible exactly where it happened.
   Show waits, timers, and events from V3 inline.

4. **Execution tree (U4).** Render `GET /runs/{id}/tree`: parent and child
   relationships, child workflows as nested runs, and map items grouped under
   their fan-out. This reads the V4 linkage directly.

5. **Task detail (U5).** Render `GET /runs/{id}/tasks/{identity}`: the logical task
   with each physical attempt beneath it, inputs and outputs, the native stack
   trace on a failed attempt, the retry reason and delay, the duration, and the
   recorded model/token/cost usage from the V2 slot. A task that self-reported no
   usage simply shows none, which is the expected ADR-0008 behavior.

6. **Redaction in the UI.** Confirm redacted fields arrive already stripped from
   N18 and render as redacted, so no sensitive value reaches the browser.

7. **Demo and tests.** Open a run in Studio, walk its timeline, drill into the
   execution tree, expand a task to its attempts and stack trace, and confirm the
   V1 interruption is visible. Behavior assertions run against the read API
   payloads that back each view.

---

## Demo

The local server serves Studio on localhost. Open a run to see its timeline, drill
into the execution tree, and expand a task to see its attempts, stack trace, and
recorded usage. The V1 interruption is visible in the timeline.

---

## Test Plan

Studio is verified through its JSON read API, not through UI rendering, in the MVP
(ADR-0011). Because of that, V6's tests are not end-to-end in the browser sense; they
are view-model / read-API assertions, so the top tier is labelled **Acceptance (via
the read API)** rather than "E2E." The genuinely V6-specific content is the
view-model *transforms* (interruption marker, tree grouping, attempt grouping,
no-usage rendering); tests that merely re-exercise V5 read endpoints or redaction are
dropped as duplicates. View-models assert on the fields they need and tolerate
added/unknown contract fields, so they do not break when V7 extends the contract
(ADR-0018, H3). **Explicit non-goal:** there is no smoke test that the SPA bundle
builds and loads in a browser — ADR-0011 verifies Studio through JSON, not UI, so this
gap is intentional, not accidental.

### Acceptance Tests (via the read API)

- Every V6 view (run list, timeline, tree, task detail) is built from the V5 read-API
  payloads, with no new runtime behavior introduced (no browser rendering asserted).
- The V1 interruption and resume are visible in the timeline view-model, surfacing
  the shared marker computation owned in V1 (Q42; the marker is the presence of a
  `WorkflowResumed` event per Q52).
- Task detail distinguishes a logical task from its physical attempts and shows
  inputs, outputs, native stack traces, retry reason and delay, and duration.
- Usage metadata appears when a task self-reported it and is absent otherwise.
- Sensitive fields arrive already redacted from the V5 read API (N18); V6 renders
  them redacted and does not re-test V5's redaction.

### Integration Tests

- The run-list view model is built from `GET /runs` and orders runs most-recent-first.
- The timeline view model surfaces the interruption marker from the shared
  read/view-layer computation (it does not re-implement detection here).
- The tree view model groups map items and nests child workflows.
- The task-detail view model groups physical attempts under the logical task with
  its usage slot.

### Unit Tests

- A task with no usage slot renders no usage.

---

## Dependencies

- **Upstream:** V5 (the read API and redactor Studio consumes), and by extension
  the journal and views built in V1 through V4.
- **Downstream:** V7 adds fork, compare, and the version-mismatch banner as Studio
  views; V8 folds Studio into the single `satay dev` command.
