# ADR-0028 — Forking from code: `before_task=` fork points and the `workflow_input=` override

- **Status:** Accepted
- **Date:** 2026-08-16
- **Deciders:** Jian (leejianrong2@gmail.com)

Extends [ADR-0004](0004-append-only-journal.md) (fork = copy a prefix of an immutable
journal; terminal runs only) and reconciles the input override with
[ADR-0022](0022-nondeterminism-policy-split.md) (replay divergence is strict by
default). Motivated by [ADR-0025](0025-positioning-agents-first.md): fork, replay and
compare are the wedge, so they have to be reachable from ordinary Python.

## Context

Fork worked, and worked well — re-cutting one task of a real dossier run cost $0.0006
against $0.2750 for the whole run, and the source stayed byte-for-byte identical. It
was also close to unusable from code.

**Choosing where to cut was journal archaeology.** "Fork before synthesize" had to be
written as a scan over raw event sequence numbers:

```python
synthesis_seq = min(e.seq for e in events
                    if e.type is EventType.TASK_SCHEDULED
                    and e.payload.get("task_name") == "synthesize")
fork_point = max(e.seq for e in events if e.seq < synthesis_seq)
```

**There was no way to fork with a different input.** A fork rehydrated the source's
recorded `WorkflowCreated` input, by design. So the actual V7 user story — "re-run this
dossier with a sharper prompt" — was only expressible by hoisting the prompt out of the
workflow input into module-global mutable state (`SYNTHESIS_STYLE = {"value": ...}`).
That is the opposite of how anyone would design the workflow: the prompt belongs in the
brief. A runtime that makes the natural design unusable is teaching the wrong lesson.

**And driving a fork took four objects**: a `ControlAPI`, a `CommandQueue`, a
`worker.tick()`, and a no-op `satay.start(run_id=fork_id)` to read the result.

## Decision

### 1. `satay.fork(...)` is public core API

```python
handle = await satay.fork(run_id, before_task="synthesize", workflow_input=brief)
print(await handle.result())
```

It lives in the **core**, not `satay[studio]`. The wedge cannot require the extra. The
seeding and resolution logic stays in `satay.control.commands` — pure Python for
precisely this reason (ADR-0016) — and is imported lazily, the same arrangement
`RunHandle.cancel()` already uses; `import satay` still pulls no FastAPI, uvicorn,
Pydantic, Typer or Click.

The in-process caller writes directly, exactly as `satay.start` does. ADR-0012's
single-writer rule is about the HTTP thread not writing behind the worker's back, and
that is unchanged: the HTTP fork route still enqueues a `ForkRun` for the worker. Both
paths converge on one `drive_forked_run`, so a fork driven from code and one driven
from Studio take the identical path, and neither records `WorkflowResumed` — a fresh
fork is a new run, not a crash recovery, so it carries no ⚡ (ADR-0009/Q52).

### 2. `before_task=` selects the fork point; ambiguity resolves to the earliest

`before_task="synthesize"` cuts so that the fork's copied prefix ends **immediately
before** that task was scheduled, so it re-runs.

- **A name that ran N times selects the earliest occurrence**, and is not an error.
  Cutting later would leave results from the earlier occurrences in the prefix,
  recorded under exactly the code or input being changed — a half-updated run is a
  worse default than an over-complete one, and the extra work is bounded and visible in
  the execution counts. It is also what every hand-written version of this scan did
  (`min(seq)`), so it matches demonstrated intent. Name a specific occurrence with
  `before_ordinal=`, which is the `ordinal` half of the `task:ordinal` identity Studio
  and `compare` already show; keyed fan-out items have no ordinal and are not
  selectable this way.
- **A name that never ran is a `ForkValidationError` listing the names that did.** The
  error tells you what to type instead, which is the entire difference between a
  usable and an unusable affordance.
- `fork_point_seq=` remains, inclusive, as the raw form Studio sends from a clicked
  event. Exactly one of the two is required.

### 3. `workflow_input=` overrides the input, and is written into the fork's journal

Not passed at drive time. The override replaces `input_ref` on the fork's **own copy**
of `WorkflowCreated`, and the fork's `RunForked` lineage gains `input_overridden: true`
and the source's `source_input_ref`.

Writing it down is what makes it durable. A fork that parks on a timer and is woken by
the poll loop, or crashes and is resumed, reads its input back from its own journal; an
override held only in memory would silently revert to the source's input on the next
wake — a wrong answer reported as success, the failure mode ADR-0022 exists to prevent.
It also keeps the run honest for the debugger: the fork's timeline shows the input it
actually ran under, and its lineage records that the input was not inherited.

This does not rewrite history. ADR-0004's rule is that a run's journal is immutable;
the fork is a **new** run, and this decides what its seeded copy says at creation time,
before anything has read it. The source is untouched and still records its own input.

### 4. The override applies to the un-replayed suffix only, and strict stays strict

This is the real design question, and the answer follows from what a fork *is*.

- **The copied prefix is still reconciled, unchanged.** Every durable call recorded in
  the prefix is a journal hit and is **not** re-executed, whatever the new input says.
  That is not a compromise, it is the definition of a fork and the source of its value:
  the $0.2750 run becomes a $0.0006 re-cut precisely because the prefix is treated as
  history rather than as a prediction.
- **So only calls after the fork point see the new input.** The rule the docs state
  plainly: *put the fork point before the first durable call that should see the new
  input*, and `before_task=` is the tool for saying exactly that.
- **Strict nondeterminism is not relaxed for forks.** If the new input would have made
  the workflow issue *different tasks* inside the copied prefix, replay diverges and
  `NondeterminismError` raises under the default policy. Keeping strict here is the
  whole point: the prefix is a factual record of calls that would not have happened
  under this input, and reusing it anyway would splice two incompatible histories into
  a plausible wrong answer. The remedy is in the user's hands and is cheap — fork
  earlier, before the divergence.
- **Detection still compares the schedule, not arguments** (ADR-0003/0022 unchanged).
  A new input that changes only the *arguments* of prefix calls is not detected, and by
  the rule above it must not be: those calls already happened. This is a documented,
  tested property, not an oversight — the test asserts that forking after `research`
  with a new topic keeps the old notes, and that forking before it picks the new topic
  up.
- **One case is refused outright.** A fork point at or past the source's terminal event
  copies the whole finished run, so the engine's idempotent-terminal short-circuit
  fires and nothing re-executes. Combined with an override that means recording a new
  input and then handing back the old result — silently. `create_fork` rejects it with
  an error naming the fix. Without an override the same fork point is still allowed: it
  is a pure clone, useless but not misleading.

## Consequences

- "Fork before the bad call, with a sharper prompt" is three readable lines, and the
  prompt can live in the workflow input where it belongs. Global mutable state is no
  longer the price of a fork demo. KAN-656 builds on this.
- One more public name (`satay.fork`) and two more fork parameters to document. The
  five durable primitives are untouched; `fork` is a control operation on a finished
  run, not a primitive a workflow body calls.
- `RunForked` payloads gain two optional keys. Additive, present only when an input was
  overridden, so existing readers and the `forked_from` view are unaffected
  (ADR-0018).
- The HTTP `POST /runs/{id}/fork` body is unchanged (`fork_point_seq`), since Studio
  resolves the fork point by clicking. Exposing `before_task=` / `workflow_input=` over
  HTTP is a small follow-up once there is a UI that wants them.
- Forking a **fork** with a new input works and composes; lineage is still one hop at a
  time through the max-`seq` `RunForked`.
