# Forking a Run

A fork copies a finished run's journal up to a chosen point into a brand new run, then
drives that new run forward under whatever your code and your input say *now*. Everything
before the fork point is replayed off the journal. Everything after it actually runs again.

The source run is never touched. That is the property the whole feature rests on: you keep
the old answer to compare against, and you pay only for the part you changed.

```python
import satay

handle = await satay.fork(run_id, before_task="synthesize")
print(await handle.result())
```

That is the whole thing. No control API, no command queue, no worker tick. `satay.fork` is
core — it does not need the `satay[studio]` extra.

For the loop end to end — a run that completes and is wrong, a fork before the bad call, and a
call-by-call comparison of the two — see the
[Fork, Replay, Compare](cookbook/fork-and-compare.md) recipe. It is a runnable file, and the
output on that page is what it printed.

## Choosing where to cut

Pass exactly one of `before_task=` or `fork_point_seq=`.

**`before_task="synthesize"`** cuts so that task re-runs: the copied prefix ends
immediately before the task was scheduled. This is the one you want almost always, because
it says what you mean — *the thing after this point is what I am changing*.

**`before_ordinal=`** names which occurrence, when a task ran more than once:

```python
# The third call to `draft`, counting from zero — the same ordinal Studio and
# compare show as `draft:2`.
handle = await satay.fork(run_id, before_task="draft", before_ordinal=2)
```

Without `before_ordinal`, a task that ran N times resolves to its **earliest** occurrence.
That is deliberate. Cutting later would leave results from the earlier occurrences sitting
in the prefix, recorded under exactly the code or prompt you are trying to change, and a
half-updated run is a worse thing to be handed than an over-complete one.

A name that never ran is an error that lists the names that did:

```pycon
>>> await satay.fork(run_id, before_task="sythesize")
ForkValidationError: run 'cedd8fb7…' never scheduled a task named 'sythesize';
it ran 'plan', 'research', 'synthesize'
```

**`fork_point_seq=`** is the raw form — an event sequence number, inclusive, meaning "keep
through this event". It is what Studio sends when you click "fork before here", and it is
there when you need a cut that is not expressible as a task boundary.

Keyed fan-out items (`map` with `key=`) have no ordinal and cannot be selected with
`before_ordinal`; use `fork_point_seq=` for those.

## Forking with a different input

`workflow_input=` runs the fork under a different input. This is the "same run, sharper
prompt" loop, and it means the prompt can live in the workflow's input where it belongs
rather than in a module-level global:

```python
handle = await satay.fork(
    run_id,
    before_task="synthesize",
    workflow_input={"topic": "acme corp", "style": "sceptical"},
)
```

The override is written into the fork's own `WorkflowCreated` event, so it is durable: a
fork that parks on a timer and wakes later, or crashes and resumes, reads back the input it
actually ran under. The fork's lineage records `input_overridden`, and the source run still
records its own original input.

### What the override does and does not reach

**Only calls after the fork point see the new input.** The copied prefix is history — those
calls already happened, under the old input, and a fork reuses them rather than re-running
them. That reuse is exactly why a fork is cheap.

So the rule is one sentence: **put the fork point before the first durable call that should
see the new input.**

```python
# Changing the topic but forking after `research` keeps the OLD research notes.
# The synthesis is re-cut; the notes it works from are not.
await satay.fork(run_id, before_task="synthesize", workflow_input={"topic": "…"})

# Forking before `research` re-runs the research too, under the new topic.
await satay.fork(run_id, before_task="research", workflow_input={"topic": "…"})
```

!!! warning "Changing data is safe. Changing the schedule is not"

    [Nondeterminism detection](determinism.md) is **strict by default** and compares the
    durable-call *schedule* — which tasks ran, in what order. A prompt or a topic flowing
    through a call that still happens in the same place leaves the schedule identical, so
    the fork replays cleanly.

    An input that makes the workflow call *different tasks* inside the copied prefix is a
    real divergence, and it raises `NondeterminismError` rather than splicing two
    incompatible histories into a plausible-looking wrong answer. The divergent call never
    executes and nothing is recorded. The fix is to fork earlier, before the branch:

    ```pycon
    >>> await satay.fork(run_id, fork_point_seq=31, workflow_input={"mode": "shorten"})
    >>> await handle.result()
    NondeterminismError: durable call 1 diverged: journal recorded 'polish', code issued 'shorten'
    ```

    Detection compares the schedule and not the arguments, so an input that only changes
    the *arguments* of prefix calls is not flagged — and by the rule above it must not be.
    Those calls already happened.

A fork point at or past the source's terminal event copies the whole finished run, so
nothing would re-execute. Combined with `workflow_input=` that would mean recording a new
input and then handing back the old result, so it is refused with an error rather than
silently ignored.

## Everything else about forks

- **Terminal runs only** — completed, failed, or cancelled ([ADR-0004](decisions.md)).
  Forking a run that is still executing is not supported.
- **A fork shares blob files with its source run**, and there is no blob garbage collection
  ([limits](limits.md)).
- **A fork is a new run, not a crash recovery.** It records no `WorkflowResumed`, so it
  carries no `⚡` in the timeline.
- **The fork is stamped with the current code version**, since the whole point is to re-run
  under whatever your code says now.
- **Forks of forks compose.** Lineage is one hop at a time; follow `forked_from` on the run
  header or in the timeline JSON.
- **Compare** a fork against its source to see, call by call, which results were replayed
  from the journal and which were re-run. See [Studio](studio.md#fork), or
  [Fork, Replay, Compare](cookbook/fork-and-compare.md) for the same table printed from
  Python.

## From Studio and over HTTP

Studio offers "fork before here" on every timeline event, and `POST /runs/{id}/fork` takes a
`fork_point_seq`. Those writes go through the command queue and are applied by the worker,
which stays the single writer. The in-process `satay.fork` writes directly, exactly as
`satay.start` does, and both paths seed and drive a fork identically.

The reasoning behind the fork-point selection and the input-override semantics is recorded
in [ADR-0028](decisions.md).
