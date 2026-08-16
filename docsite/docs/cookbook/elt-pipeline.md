# An ELT Pipeline

The workload most people actually have. Five sources extracted in parallel, transformed, and
loaded into a warehouse. The worker dies mid-load. One load loses its acknowledgement and
retries. One source is too wide to fit in a journal row. One source is corrupt.

This is the longest recipe, and roughly half of it is about what Satay does **badly** today. The
fail-fast section and the idempotency caveat are the parts worth your time, because they are the
ones that will bite you in a real nightly load.

Source: [`examples/elt_pipeline_demo.py`](https://github.com/leejianrong/satay-runtime/blob/v0.1.0a3/examples/elt_pipeline_demo.py)
(687 lines, so this page excerpts it)

## Get It And Run It

```bash
pip install 'satay[studio]'
curl -fsSL -O https://raw.githubusercontent.com/leejianrong/satay-runtime/v0.1.0a3/examples/elt_pipeline_demo.py
SATAY_DATA_DIR=.satay-demo python elt_pipeline_demo.py
```

Everything is local. The file writes its own source CSVs and its own SQLite warehouse into the
data directory, so there is no network anywhere and nothing to configure.

## The Pipeline

```python
@satay.workflow
async def elt_pipeline(sources: list[Source]) -> PipelineReport:
    """Extract every source, transform each, load each — three keyed fan-outs."""
    raw = await satay.map(extract, sources, key=source_key, concurrency=1)
    batches = await satay.map(transform, raw, key=extracted_key, concurrency=1)
    reports = await satay.map(load, batches, key=batch_key, concurrency=1)
    widest = max(raw, key=lambda item: len(item.text))
    return PipelineReport(reports=reports, widest_sample=widest.text)
```

Three keyed fan-outs in sequence. Fifteen durable calls for five sources, each one recoverable on
its own.

The stages that read and compute declare nothing special:

```python
@satay.task()
async def extract(source: Source) -> Extracted:
    """Pull one source. A read, so no ``side_effect=`` declaration is needed."""
    record("extract", source.source_id)
    return Extracted(source_id=source.source_id, text=read_source(source.source_id))


@satay.task()
async def transform(raw: Extracted) -> Batch:
    """Parse one source's text into rows. Pure, and durable all the same."""
    record("transform", raw.source_id)
    return Batch(source_id=raw.source_id, rows=parse(raw.source_id, raw.text))
```

The stage that writes declares a great deal:

```python
@satay.task(retries=1, side_effect=True, idempotent=True)
async def load(batch: Batch) -> LoadReport:
    """Write one source's rows to the warehouse, exactly once per logical call."""
    ctx = satay.task_context()
    record("load", batch.source_id)
    written = deduped = 0
    conn = sqlite3.connect(PATHS["warehouse"])
    try:
        for row in batch.rows:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO warehouse (load_key, source_id, record_id, body) "
                "VALUES (?, ?, ?, ?)",
                (f"{ctx.idempotency_key}#{row.record_id}", row.source_id, row.record_id, row.body),
            )
            written += cursor.rowcount
            deduped += 1 - cursor.rowcount
        conn.commit()
    finally:
        conn.close()
```

`side_effect=True` says this task touches the outside world. `idempotent=True` is a **promise**
that it keys that effect on `ctx.idempotency_key`, which the `INSERT OR IGNORE` against a unique
index is what actually delivers. Break the promise and nothing stops you; keep it and a retry is
harmless.

!!! tip "Look at the composed key"

    The `load_key` written per row is `ctx.idempotency_key` **plus** the record id, joined with
    a `#`. That is deliberate: `ctx.idempotency_key` identifies one *logical call*, not one row.
    A loader writing four rows needs four distinct dedupe keys, so it composes them by hand.
    Get this wrong and one row's insert suppresses the other three. This comes up again below.

## Sections 1 And 2: The Crash And The Lost Ack

```console
$ SATAY_DATA_DIR=.satay-demo python elt_pipeline_demo.py
Satay — an ELT pipeline: fan-out extract, idempotent load, blob spill
data dir:  …/.satay-demo
sources:   ['crm-contacts', 'orders', 'clickstream', 'billing', 'inventory'] (files in …/.satay-demo/sources)
warehouse: …/.satay-demo/warehouse.db

1) extract → transform → load, with the worker dying mid-load
   phase 1: the loader arms a crash the moment `orders` commits
     load crm-contacts  attempt 1: 3 row(s) written, 0 already keyed in (ignored)
     load orders        attempt 1: 4 row(s) written, 0 already keyed in (ignored)
     load orders        attempt 2: 0 row(s) written, 4 already keyed in (ignored)
     worker died: simulated crash after event 'TaskCompleted'
     durably loaded before the crash: ['crm-contacts', 'orders']
   phase 2: resume the same run — only the unresolved sources run again
     load clickstream   attempt 1: 1 row(s) written, 0 already keyed in (ignored)
     load billing       attempt 1: 2 row(s) written, 0 already keyed in (ignored)
     load inventory     attempt 1: 2 row(s) written, 0 already keyed in (ignored)
     status: completed   run 60c7453d26484d458127ebfdd6608162

   per-source ledger
     source         extract   transform   load               warehouse
     crm-contacts   phase 1   phase 1     phase 1 (REUSED)   3 row(s), 3 distinct
     orders         phase 1   phase 1     phase 1 (REUSED)   4 row(s), 4 distinct
     clickstream    phase 1   phase 1     phase 2            1 row(s), 1 distinct
     billing        phase 1   phase 1     phase 2            2 row(s), 2 distinct
     inventory      phase 1   phase 1     phase 2            2 row(s), 2 distinct

     15 durable calls, 16 task bodies executed, 1 crash, 1 lost ack.
     The one extra body is `orders`' load retry — at-least-once, by design.
     Every source holds exactly as many warehouse rows as it has records.

2) what the idempotency key is actually for
   the same lost ack, loaded by a task that ignores ctx.idempotency_key:
     careless load attempt 1: 4 rows INSERTed
     careless load attempt 2: 4 rows INSERTed
     unkeyed loader: 8 rows for 4 records  ← every record duplicated
     keyed loader:   4 rows for 4 records  ← same lost ack, no damage
     Satay guarantees at-least-once. The key is the half you have to write.
     `effect_safety=strict` refuses to even schedule the unkeyed one.
     One caveat worth knowing: ctx.idempotency_key is sha256(run_id, task, key),
     so it dedupes retries and resumes *of one run* — not a second run over the
     same data. For that, give the run itself a key: satay.start(...,
     idempotency_key='nightly-2026-01-01'), which resolves to the same run.
```

Three lines carry section 1.

**`load orders attempt 2: 0 row(s) written, 4 already keyed in (ignored)`.** The classic
ambiguous completion: the warehouse committed the rows and the acknowledgement never came back:

```python
if PHASE["n"] == 1 and batch.source_id == LOST_ACK_SOURCE and ctx.attempt == 1:
    # The classic ambiguous completion: the warehouse committed, the ack did not
    # arrive. Satay cannot know the write landed, so it retries — at-least-once.
    raise ConnectionError("warehouse committed the rows but the ack never came back")
```

Satay cannot know whether the write landed, so it retries. That is what at-least-once means. The
second attempt re-derives the same idempotency key, its four inserts hit the unique index, and
`0 row(s) written` is the loader saying its own retry was a no-op.

**`15 durable calls, 16 task bodies executed`.** Fifteen logical calls, sixteen physical
executions. The extra one is that retry. There is no configuration that makes those numbers
equal, and pretending otherwise is how you double-charge a customer.

**`load ... phase 1 (REUSED)` for `crm-contacts` and `orders`.** The resume only re-ran the three
loads that had not committed. Extract and transform for all five had already committed in phase 1
and were reused wholesale, which is why the `extract` and `transform` columns say `phase 1` for
every row.

### What The Key Buys You

Section 2 runs the loader everybody writes first:

```python
@satay.task(retries=1, side_effect=True)
async def load_carelessly(batch: Batch) -> int:
    """The loader everybody writes first: a plain INSERT that ignores the idempotency key."""
    ctx = satay.task_context()
    record("load_carelessly", batch.source_id)
    conn = sqlite3.connect(PATHS["warehouse"])
    try:
        conn.executemany(
            "INSERT INTO warehouse (load_key, source_id, record_id, body) VALUES (NULL, ?, ?, ?)",
            [(row.source_id, row.record_id, row.body) for row in batch.rows],
        )
        conn.commit()
    finally:
        conn.close()
```

Same lost ack, same retry, no key. Eight rows for four records. Every record duplicated.

Note what it does not declare: `idempotent=True`. Satay knows this shape is unsafe and says so.
The example sets `effect_safety="warn"` on that run specifically so the damage can happen where
you can see it, and the warning still goes to stderr:

```console
effect_safety: task 'load_carelessly' is side-effecting and retryable but declares no
idempotency or compensation strategy (set @task(idempotent=True) or accept a ctx parameter)
```

`warn` is the default. Set `effect_safety=strict` and that task is refused at schedule time
rather than warned about. On a pipeline that writes to a warehouse, `strict` is the setting you
want.

!!! warning "`ctx.idempotency_key` does not survive a re-trigger"

    The key is `sha256(run_id, task_name, map_key)`. **The run id is in it.** So it dedupes:

    - retries of one logical call, and
    - resumes of one run.

    It does not dedupe a second, separate run over the same data. An operator who re-triggers
    last night's load gets a new run id, therefore new keys, therefore a second copy of every
    row. The dedupe you thought you had is not there.

    The fix is to key the **run**, not just the task:

    ```python
    satay.start(elt_pipeline, SOURCES, idempotency_key="nightly-2026-01-01")
    ```

    A repeated `idempotency_key` on `start` resolves to the same logical run rather than creating
    a duplicate, so the re-trigger becomes a resume and the task keys come out identical. If the
    run had already completed, the repeated key is a no-op that hands back the recorded result.
    Derive that key from the logical batch date and pass it on every scheduled invocation.

    And remember the row-level half from earlier: one call key covers one call, so a multi-row
    effect still has to compose `key#record_id` itself.

## Section 3: Payload Spill

```console
3) blob spill — one source is too wide for a journal row
     clickstream extracted 300,005 characters; the spill threshold is 262,144 bytes
     journal row  : {"task_name":"extract","key":"clickstream","output_ref":{"$satay":"blobref","id":"b7a2f9f8d0c52923d224cd689f47e698709af4bf6a58cc2f404f17440bafb5a9","size":300103}}
                    (163 bytes on the row — a reference, not the value)
     blob on disk : blobs/b7a2f9f8d0c5….blob  (300,103 bytes, sha256-addressed)
     store.read_events()  → 300,005 chars, sha 144beb286ab64244
     handle.result()      → 300,005 chars, sha 144beb286ab64244
     5 blob(s) written in total; the workflow body never mentions blobs.
     The resumed run in section 1 read this source's value back out of the blob
     to build its report — spill survives a crash as transparently as it is written.
```

A task output over 256 KiB does not go into the journal row. It goes to a content-addressed file
under `blobs/`, and the row keeps a `blobref` naming it by content hash. The row is 163 bytes; the
value is 300 KB.

The reason to care is the last line. That resumed run in section 1 rebuilt its report from this
source's full text, which it read back out of the blob after a crash. Spill is transparent on
write and on read, in the workflow body and in `handle.result()` alike. Both hashes above match,
which is the example proving it rather than asserting it.

The example reads the raw row with plain `sqlite3` on purpose, because going through the store
would rehydrate the blob and hide exactly what it is trying to show.

!!! info "There is no blob garbage collection"

    Blobs are never collected, runs are never deleted, and the journal is never compacted
    ([ADR-0004](../decisions.md)). A fork also shares blob files with its source run, so any
    future collector has to be reference-aware. On a pipeline that spills a 300 KB payload every
    night, plan for the disk. [Limits](../limits.md) has the full list of what is absent.

## Section 4: Fail-Fast, And What It Costs

This is the section to read twice.

!!! success "This is the section that got the feature built"

    The transcript below is what the runtime did when this recipe was written, and it is still
    what the **default** does. It is also the evidence that produced
    [ADR-0027](../decisions.md): `map` and `gather` now take `return_exceptions=True`, which
    keeps the five completed extracts *and* records the sixth source's failure in the journal as
    a terminal `TaskFailed`. Read sections 4 and 5 as the argument, then use collect mode —
    section 5's outcome-returning workaround is the anti-pattern collect mode replaces, and it is
    no longer the advice.

```console
4) one source fails — fan-out is fail-fast (ADR-0020)
     the map raised ValueError: ledger-eu: malformed record on line 2: 'ledger-eu-002'
     status: failed   run dcb9a00ea9a44acd8d18bf26a6d53e7a
     sibling extracts that COMPLETED anyway: ['billing', 'clickstream', 'crm-contacts', 'inventory', 'orders']
     …and are now unreachable: 300,262 characters of finished work,
       including the 300 KB source, thrown away because one sibling raised.
     The results are on the journal, but the run is terminal: `satay.start(
       run_id=…)` on a failed run re-raises rather than resuming. Forking the
       run is the only way back in, and there is no `return_exceptions` mode.
     journal: {'WorkflowCreated': 1, 'TaskScheduled': 6, 'TaskAttemptStarted': 6, 'TaskCompleted': 5, 'TaskAttemptFailed': 1, 'WorkflowFailed': 1}
     Read that tally again: the successes are recorded. Only the workflow lost.
```

Six sources. One is corrupt. The `map` raises and the run is `failed`.

Now look at the tally: **five `TaskCompleted`**. Five extracts finished, committed their results,
and are sitting on the journal right now. The run cannot reach them, because a failed run is
terminal and `satay.start(run_id=...)` on it re-raises rather than resuming.

That is 300,262 characters of completed work, including the whole 300 KB source, made unreachable
by one sibling raising. Under the fail-fast default ([ADR-0020](../decisions.md)) forking the run
is the only way back in.

For an extract fan-out this is often the wrong default. "Five of six sources loaded, quarantine the
sixth" is usually what you wanted — which is exactly what
[collect mode](../primitives.md#failure-fail-fast-or-collect) now gives you:

```python
outcomes = await satay.map(
    extract, sources, key=lambda s: s.source_id, return_exceptions=True
)
extracted = [o for o in outcomes if not isinstance(o, Exception)]
quarantined = [o.key for o in outcomes if isinstance(o, satay.TaskFailedError)]
```

Five results, one `TaskFailedError`, a `completed` run, and a `TaskFailed` event in the journal
naming `ledger-eu`. No fork required.

## Section 5: The Workaround, And Its Price

Before collect mode existed there was exactly one way to get partial results: stop raising. It is
kept here because the price it charges is the reason collect mode records `TaskFailed` — do not
copy this pattern into new code.

```python
@satay.task()
async def extract_outcome(source: Source) -> Outcome:
    """The same extract, but it never raises — it reports."""
    record("extract_outcome", source.source_id)
    try:
        text = read_source(source.source_id)
        parse(source.source_id, text)
    except (OSError, ValueError) as exc:
        return Outcome(source_id=source.source_id, ok=False, text="", error=str(exc))
    return Outcome(source_id=source.source_id, ok=True, text=text, error="")


@satay.workflow
async def resilient_extract(sources: list[Source]) -> list[Outcome]:
    """Fan out with the outcome-returning task; quarantine the bad source and carry on."""
    return await satay.map(extract_outcome, sources, key=source_key)
```

```console
5) the workaround: return an outcome instead of raising
     status: completed   run 92c07a164d8c4ee2a643fd360f63ca59
     extracted: ['crm-contacts', 'orders', 'clickstream', 'billing', 'inventory']
     quarantined: ledger-eu — ledger-eu: malformed record on line 2: 'ledger-eu-002'
     5/6 sources survived, and the pipeline can go on.
     The cost: you hand-roll the union type, the try/except and the partition,
     and you give up ever seeing the failure as a failure on the journal.
```

It works. Five of six sources survive and the pipeline continues. Now the bill.

**You hand-roll the union type.** And its shape is constrained in a way that is not obvious:

```python
@dataclass(frozen=True)
class Outcome:
    """A result-or-error union, hand-rolled — the fail-fast workaround.

    Deliberately flat rather than ``Extracted | None`` plus ``Exception | None``: a union
    annotation decodes back to a plain dict on resume, so the typed shape you wrote is not
    the shape you get. Flat fields survive.
    """

    source_id: str
    ok: bool
    text: str
    error: str
```

A task's **return annotation** is what rehydrates a recorded result into your class on resume, and
it no longer has to be a bare concrete type to manage it. `X | None`, `Optional[X]`, `X | Y`,
`list[X]`, `dict[str, X]`, `tuple[X, Y]`, `Annotated[X, ...]` and any nesting of those all
rehydrate to the type the first execution produced, so nothing forces `Outcome` to be flat. A
resumed `Extracted | None` is an `Extracted`, not a dict.

What a union does still owe you is a way to tell its arms apart. The codec picks the arm from the
recorded shape and, for objects, the field-name set; two dataclass arms with identical fields are
ambiguous, and an annotation it cannot resolve raises `DecodeError` naming the annotation rather
than quietly handing back a dict on the recovery path only. Failing loudly is the change that
matters — the old behaviour was a wrong type you found out about on the resume.

!!! note "The docstring above is out of date"

    `Outcome`'s docstring is quoted verbatim from the example, and it still explains the flat shape
    as a workaround for union annotations decoding to a plain dict. That stopped being true when the
    codec learned to walk composite annotations. The flat shape is now a style choice, and it keeps
    working; it is just no longer forced.

**You hand-roll the try/except and the partition.** Every caller of every outcome-returning task
has to remember to split `ok` from not-`ok`. Miss one and a quarantined source flows downstream as
an empty string.

**And you go blind.** This is the part that matters most. A quarantined failure is recorded as
`TaskCompleted`, because as far as the runtime is concerned the task succeeded: it returned a
value. So:

- retries stop applying, since a task that returns is a task that succeeded, and a transient
  read error is now permanently quarantined instead of retried;
- the run status is `completed`, so nothing you monitor on run status will ever alert;
- Studio shows a green run with no failed attempts;
- the journal has no record that anything went wrong, only an `Outcome` payload with `ok=False`
  that you have to know to look inside.

You have traded a loud, well-recorded failure that destroys the run for a silent, unrecorded one
that does not. Sometimes that is the right trade. Make it deliberately, and put the quarantine
count somewhere your alerting can see it, because the runtime will not do it for you.

## Open It In Studio

```bash
satay dev --data-dir .satay-demo
```

Open the printed URL with its `?token=` query string. The data directory holds four runs, and the
run list is worth reading as a set: the completed pipeline, the careless load, the failed
`strict_extract`, and the completed `resilient_extract`.

Three things to click:

1. **The `elt_pipeline` execution tree.** Three `map` calls, five keyed items under each. The
   shape of the pipeline, rather than fifty interleaved events.
2. **`strict_extract`, the failed run.** The timeline shows five `TaskCompleted` and one
   `TaskAttemptFailed`. That is the fail-fast cost rendered as a picture: all that green, and the
   run still died.
3. **`resilient_extract` next to it.** Six `TaskCompleted`, run `completed`, no failures anywhere.
   The quarantined source is invisible unless you expand its output and read `ok=False`. That is
   what going blind looks like in the UI.

Compare those last two side by side and the tradeoff stops being abstract.

## Recap

- Three keyed fan-outs in sequence recover independently. A crash mid-load re-runs only the loads
  that had not committed.
- At-least-once is real: 15 durable calls, 16 executions, and the extra one wrote to a warehouse.
- `side_effect=True, idempotent=True` plus a `ctx.idempotency_key`-derived unique key is what
  makes the retry harmless. The unkeyed loader duplicates every record.
- `effect_safety=strict` refuses to schedule a retryable writer that has not promised
  idempotency. Default is `warn`. Pick `strict` for a pipeline that writes.
- `ctx.idempotency_key` embeds the run id, so it covers retries and resumes of one run, not an
  operator re-trigger. Pass `idempotency_key=` to `satay.start` for that. Compose `key#record_id`
  yourself for multi-row effects.
- Payloads over 256 KiB spill to content-addressed blobs, transparently on write and read, and
  survive a crash. There is no blob collection.
- Fan-out is fail-fast **by default**. One corrupt source killed a run in which five extracts had
  already committed, and forking was the only way back to them.
- The outcome-returning workaround gets you partial results and costs you retries, run-status
  alerting, and any journal record that a failure happened. That trade is what
  [ADR-0027](../decisions.md) removed: `return_exceptions=True` keeps the siblings *and* keeps the
  failure on the journal.

Next: [An Agentic DAG](agentic-dag.md), which is the same problem when each sibling is a model call
you paid for.
