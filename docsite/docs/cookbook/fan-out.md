# Fan-Out With Crash Recovery

Five documents, indexed in parallel. The worker dies after the first one. It restarts, indexes
one more, and dies again. It restarts a third time and finishes.

Total executions of the expensive step: **five**. One per document, across three worker
lifetimes. This is the demo that tends to convince people, and it is one primitive plus one
argument.

Source: [`examples/fan_out_recovery_demo.py`](https://github.com/leejianrong/satay-runtime/blob/v0.1.0a2/examples/fan_out_recovery_demo.py)

## Get It And Run It

```bash
pip install 'satay[studio]'
curl -fsSL -O https://raw.githubusercontent.com/leejianrong/satay-runtime/v0.1.0a2/examples/fan_out_recovery_demo.py
SATAY_DATA_DIR=.satay-demo python fan_out_recovery_demo.py
```

## The Workflow

```python
@dataclass(frozen=True)
class Document:
    doc_id: str
    pages: int


def document_key(doc: Document) -> str:
    """The stable fan-out identity of one item."""
    return doc.doc_id


@satay.task()
async def index_document(doc: Document) -> int:
    """Index one document. Expensive — the whole point is not to redo it."""
    key = document_key(doc)
    EXECUTIONS[key] = EXECUTIONS.get(key, 0) + 1
    INDEXED_IN_PHASE.setdefault(key, PHASE["n"])
    return doc.pages * 100


@satay.workflow
async def index_batch(docs: list[Document]) -> list[int]:
    return await satay.map(index_document, docs, key=document_key, concurrency=1)
```

That is the entire mechanism. `satay.map` fans a task out over items, and every item becomes a
**keyed durable call**, identified by `(task_name, key(item))` instead of by position.

The key is the trick. On restart each item consults the journal *for itself*. An item with a
committed result is reused; an unresolved one executes. No coordination between items is needed
because each one carries its own identity.

## What It Printed

```console
$ SATAY_DATA_DIR=.satay-demo python fan_out_recovery_demo.py
Satay — fan-out with crash recovery
data dir: …/.satay-demo
batch: ['doc-intro', 'doc-methods', 'doc-results', 'doc-discussion', 'doc-appendix']

phase 1: start the fan-out, kill the worker after the first item
  worker died: simulated crash after event 'TaskCompleted'
  durably indexed so far: ['doc-intro']
  run a5234dbcd008404db0e7ef895802a1d4

phase 2: restart the same run — then kill it again after one more item
  worker died: simulated crash after event 'TaskCompleted'
  durably indexed so far: ['doc-intro', 'doc-methods']
  reused from the journal (never re-indexed): ['doc-intro']
  newly indexed in phase 2: ['doc-methods']

phase 3: restart with no fault — the run finishes
  result: [300, 1100, 700, 900, 200]
  status: completed
  results rejoin in INPUT order, not completion order

per-document ledger
  document         indexed in   executions  on the final restart
  doc-intro        phase 1      1           REUSED from the journal
  doc-methods      phase 2      1           REUSED from the journal
  doc-results      phase 3      1           ran now
  doc-discussion   phase 3      1           ran now
  doc-appendix     phase 3      1           ran now

  5 documents, 2 crashes, 5 executions in total.
  Every document was indexed exactly once. That is the guarantee.
  TaskCompleted on the journal: 5 — one per key, 5 distinct.
  WorkflowResumed events: 2 — the two ⚡ markers below.

Run a5234dbcd008404db0e7ef895802a1d4 — 19 event(s)
    1  2026-07-31T07:40:18.002836+00:00  WorkflowCreated  workflow=index_batch code_version=git:4d22d57c0a914532d987bc7df2af0f65530cdce6
    2  2026-07-31T07:40:18.007733+00:00  TaskScheduled  task=index_document key=doc-intro
    3  2026-07-31T07:40:18.012354+00:00  TaskAttemptStarted  task=index_document key=doc-intro attempt=1
    4  2026-07-31T07:40:18.016921+00:00  TaskCompleted  task=index_document key=doc-intro
⚡   5  2026-07-31T07:40:18.023035+00:00  WorkflowResumed
    6  2026-07-31T07:40:18.028021+00:00  TaskScheduled  task=index_document key=doc-methods
    7  2026-07-31T07:40:18.032923+00:00  TaskAttemptStarted  task=index_document key=doc-methods attempt=1
    8  2026-07-31T07:40:18.037664+00:00  TaskCompleted  task=index_document key=doc-methods
⚡   9  2026-07-31T07:40:18.044274+00:00  WorkflowResumed
   10  2026-07-31T07:40:18.049239+00:00  TaskScheduled  task=index_document key=doc-results
   11  2026-07-31T07:40:18.054179+00:00  TaskAttemptStarted  task=index_document key=doc-results attempt=1
   12  2026-07-31T07:40:18.059015+00:00  TaskCompleted  task=index_document key=doc-results
   13  2026-07-31T07:40:18.064034+00:00  TaskScheduled  task=index_document key=doc-discussion
   14  2026-07-31T07:40:18.069534+00:00  TaskAttemptStarted  task=index_document key=doc-discussion attempt=1
   15  2026-07-31T07:40:18.075193+00:00  TaskCompleted  task=index_document key=doc-discussion
   16  2026-07-31T07:40:18.081215+00:00  TaskScheduled  task=index_document key=doc-appendix
   17  2026-07-31T07:40:18.087257+00:00  TaskAttemptStarted  task=index_document key=doc-appendix attempt=1
   18  2026-07-31T07:40:18.093738+00:00  TaskCompleted  task=index_document key=doc-appendix
   19  2026-07-31T07:40:18.099469+00:00  WorkflowCompleted

journal kept in …/.satay-demo
open the fan-out in Studio:  satay dev --data-dir …/.satay-demo
or as text:                  satay runs show a5234dbcd008404db0e7ef895802a1d4 --data-dir …/.satay-demo
```

Read the ledger column by column. Every document has `executions = 1`. Two of them were indexed
before the final restart and came back off the journal; three ran on the last pass. Five
documents, two crashes, five executions.

## `key=` Instead Of Position

On the timeline, note what identifies each item: `key=doc-intro`, not `ordinal=3`. Compare that
to the plain tasks in the [crash-recovery recipe](crash-recovery.md), which are identified by
`(task_name, ordinal)`.

That difference is the whole reason partial-completion recovery works. If items were identified
by position, then a batch whose input order shifted between runs would match the wrong recorded
results to the wrong items. Keying on the item's own identity makes the match independent of
order and of how many siblings finished.

`key=` is required by `satay.map`, and it has to return a unique, stable, non-empty string per
item. A missing or duplicate key is a usage error raised at schedule time, not a mystery two
crashes later.

!!! warning "Derive the key from the item, never from the loop"

    ```python
    # Good: the item's own identity.
    def document_key(doc: Document) -> str:
        return doc.doc_id

    # Broken: a counter. Reorder the batch and every result matches the wrong document.
    def bad_key(doc: Document) -> str:
        return f"item-{next(counter)}"

    # Broken: a mutable field. Touch `pages` and the item loses its recorded result.
    def also_bad(doc: Document) -> str:
        return f"{doc.doc_id}-{doc.pages}"
    ```

    Stable across restarts is what makes an item reusable. If the key can change, the reuse
    silently stops working, and the symptom is a bill for work you already did.

## Results Rejoin In Input Order

`result: [300, 1100, 700, 900, 200]` matches `BATCH` position for position, even though
`doc-intro` completed in one worker lifetime and `doc-appendix` in another two crashes later.

`satay.map` returns a list in **input order** regardless of completion order. You never have to
sort the results back yourself or carry an index around inside the item to do it.

## Concurrency

The example passes `concurrency=1`, and the docstring is explicit about why:

```python
"""``concurrency=1`` here only to make the crash point deterministic for a demo (exactly
the items whose ``TaskCompleted`` committed survive). Real fan-outs leave it alone and
get the default bound of 8 in-flight items; results still rejoin in **input order**."""
```

With one item in flight, "the worker died after the first item" means exactly one item
committed, which makes the demo's ledger reproducible. Leave `concurrency` alone in real code
and you get up to 8 items running at once on the asyncio loop. The recovery behaviour is the
same either way; only the set of items that happened to commit before the crash changes.

## Two Crashes, Two Markers

There are two `⚡` markers, at sequences 5 and 9, and each one is a real worker death:

```python
async def crash_once_indexing(
    store: SQLiteStore, run_id: str | None, phase: int
) -> tuple[str, list[str]]:
    PHASE["n"] = phase
    injector = FaultInjector()
    injector.crash_after("TaskCompleted")
    handle = satay.start(index_batch, BATCH, run_id=run_id, store=store, injector=injector)
    try:
        await handle.result()
    except SimulatedCrash as exc:
        print(f"  worker died: {exc}")
    ...
```

`crash_after("TaskCompleted")` raises right after the commit, so the crash always lands with
durable state written and the process gone. Passing `run_id=None` starts a fresh run; passing the
existing id resumes it. Phase 3 does the same call with no injector at all.

## Fan-Out Is Fail-Fast

One thing this recipe does not show, because it is a happy-path demo: what happens when an item
**raises**.

A failed item raises through the `map`. In-flight siblings are allowed to settle, but their
results are discarded and the run fails. There is no collect mode and no `return_exceptions=`
([ADR-0020](../decisions.md)). If four of five documents indexed fine and the fifth raised, you
get an exception, not four results and an error.

The results of the successful siblings *are* on the journal. The run is simply terminal, so a
resume cannot reach them. That costs more than it sounds like, and the
[ELT pipeline recipe](elt-pipeline.md) puts a number on it: 300 KB of finished extraction thrown
away because one sibling raised, plus the only workaround there is today and what that workaround
costs you.

## Open It In Studio

```bash
satay dev --data-dir .satay-demo
```

Open the printed URL with its `?token=` query string, then open the `index_batch` run.

The **execution tree** is the view to use here rather than the timeline. It groups all five
`index_document` calls under their parent `map` call, each labelled with its key, so you see the
fan-out as a fan-out instead of as nineteen interleaved log lines. Click one item for its
recorded input and output.

Then go back to the timeline for the two `⚡` markers, which is where you can see how far the run
got in each worker lifetime.

## Recap

- `satay.map(task, items, key=...)` makes every item a keyed durable call, identified by
  `(task_name, key(item))`.
- On resume, each item consults the journal for itself. Committed items are reused; unresolved
  ones re-run. Crashes mid-fan-out cost only the work that had not committed.
- Derive the key from the item's own stable identity. Never a counter, a position, or a mutable
  field.
- Results rejoin in input order regardless of completion order.
- The default in-flight bound is 8. Setting `concurrency=1` is a demo trick for a deterministic
  crash point, not a recommendation.
- Fan-out is fail-fast. One raising item fails the run, sibling results become unreachable, and
  there is no collect mode.

Next: [An ELT Pipeline](elt-pipeline.md), which puts all of this into a nightly load and is
honest about the parts that hurt.
