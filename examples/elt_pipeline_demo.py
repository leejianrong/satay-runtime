"""An ELT pipeline: fan-out extract, idempotent load, blob spill, and one bad source.

The workload most people actually have. Five local sources are extracted in parallel
with ``satay.map(key=source_id)``, transformed, and loaded into a SQLite warehouse this
file creates itself. Along the way it shows the three things a durable runtime has to get
right before you trust it with a nightly load:

1. **The worker dies mid-load.** On resume the sources already loaded are reused straight
   off the journal and only the unresolved ones run again.
2. **At-least-once means you must be idempotent.** One load loses its warehouse ack and
   retries. The keyed loader writes its rows once; a second run with a loader that
   ignores ``ctx.idempotency_key`` duplicates every record. Both outcomes are printed.
3. **A source too wide for a journal row spills to a blob.** The journal keeps a
   ``blobref``; the workflow, the resume, and ``handle.result()`` all see the full value.

Then the honest part: **fan-out is fail-fast** (ADR-0020). One corrupt source raises, the
whole ``map`` raises with it, and the sibling extracts — including the 300 KB one that had
already finished — are unreachable because the run is now terminal. The last section shows
the only workaround available today: a task that returns an outcome instead of raising.

    uv run python examples/elt_pipeline_demo.py        # throwaway temp data dir
    SATAY_DATA_DIR=.satay-elt uv run python examples/elt_pipeline_demo.py

Everything is local: the source files and the warehouse are written into the data dir, and
time is a ``satay.testing.ManualClock`` so the retry backoff replays in zero wall clock.

By default the run lands in a throwaway temp directory, so this file is self-contained
wherever you download it. Set ``SATAY_DATA_DIR`` (or pass a path as the first argument)
to keep the journal, then ``satay dev --data-dir <that path>`` opens it in Studio.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import satay
from satay.blobs import SPILL_THRESHOLD_BYTES
from satay.config import DATA_DIR_ENV_VAR, blob_dir, db_path
from satay.journal.events import EventType
from satay.journal.store import SQLiteStore
from satay.testing import FaultInjector, ManualClock, SeededRng, SimulatedCrash

# -- the shapes that travel through the pipeline ---------------------------------------
#
# Frozen dataclasses round-trip through the journal codec, and — this matters — a task's
# *return annotation* is what rehydrates a recorded result back into the class on resume.
# Keep those annotations concrete: ``list[Row]`` rehydrates, ``Extracted | None`` does not
# (a union decodes to a plain dict), which is why ``Outcome`` below is flat.


@dataclass(frozen=True)
class Source:
    """One upstream system. ``source_id`` is also its fan-out key."""

    source_id: str
    records: int
    wide: bool = False


@dataclass(frozen=True)
class Extracted:
    """The raw text pulled out of one source."""

    source_id: str
    text: str


@dataclass(frozen=True)
class Row:
    """One transformed record, ready to load."""

    source_id: str
    record_id: str
    body: str


@dataclass(frozen=True)
class Batch:
    """Every row from one source, keyed by that source."""

    source_id: str
    rows: list[Row]


@dataclass(frozen=True)
class LoadReport:
    """What the loader actually did to the warehouse for one source."""

    source_id: str
    rows_written: int
    rows_deduped: int
    attempts: int


@dataclass(frozen=True)
class PipelineReport:
    """The pipeline's result.

    ``widest_sample`` is the full raw payload of the over-threshold source, carried back
    deliberately so you can see ``handle.result()`` hand you the whole value while the
    journal row behind it holds nothing but a blob reference.
    """

    reports: list[LoadReport]
    widest_sample: str


@dataclass(frozen=True)
class Outcome:
    """A result-or-error union, hand-rolled — the fail-fast workaround (see section 5).

    Deliberately flat rather than ``Extracted | None`` plus ``Exception | None``: a union
    annotation decodes back to a plain dict on resume, so the typed shape you wrote is not
    the shape you get. Flat fields survive.
    """

    source_id: str
    ok: bool
    text: str
    error: str


# -- the workload ----------------------------------------------------------------------

#: One record of ``clickstream`` is this wide, which pushes the encoded task output past
#: the 256 KiB spill threshold and into a content-addressed blob.
WIDE_RECORD_CHARS = 300_000

#: The five sources of the nightly load. Order is the fan-out order at ``concurrency=1``.
SOURCES = [
    Source("crm-contacts", 3),
    Source("orders", 4),
    Source("clickstream", 1, wide=True),
    Source("billing", 2),
    Source("inventory", 2),
]

#: A sixth source whose file is corrupt. Only sections 4 and 5 read it.
CORRUPT = Source("ledger-eu", 2)

#: The source whose warehouse write succeeds but whose ack is lost, forcing a retry.
LOST_ACK_SOURCE = "orders"

#: The source that finishes loading just as the worker dies (section 1's crash point).
CRASH_AFTER_LOADING = "orders"

#: Physical task-body executions per ``(stage, source_id)`` — the reuse marker (ADR-0011).
EXECUTIONS: dict[tuple[str, str], int] = {}

#: Which phase each ``(stage, source_id)`` first really executed in.
RAN_IN_PHASE: dict[tuple[str, str], int] = {}

#: The phase currently running, so task bodies can stamp their executions and the crash
#: only arms itself the first time through.
PHASE = {"n": 0}

#: Armed from inside the loader so the worker dies *mid-load* rather than mid-extract —
#: ``crash_after`` fires on the next matching commit, so when it is armed decides where.
INJECTOR = FaultInjector()

#: Filled in by ``main`` once the data dir is known; task bodies read them.
PATHS: dict[str, Path] = {}


def record(stage: str, source_id: str) -> None:
    """Mark one *physical* execution of a stage for a source."""
    slot = (stage, source_id)
    EXECUTIONS[slot] = EXECUTIONS.get(slot, 0) + 1
    RAN_IN_PHASE.setdefault(slot, PHASE["n"])


def source_key(source: Source) -> str:
    return source.source_id


def extracted_key(extracted: Extracted) -> str:
    return extracted.source_id


def batch_key(batch: Batch) -> str:
    return batch.source_id


# -- the local "upstream systems" and the local warehouse -------------------------------


def seed_sources(directory: Path) -> None:
    """Write the source files. No network anywhere in this example — these are the sources."""
    directory.mkdir(parents=True, exist_ok=True)
    for source in [*SOURCES, CORRUPT]:
        wide_body = "clickstream-event-" * (WIDE_RECORD_CHARS // 18)
        lines = []
        for n in range(1, source.records + 1):
            body = wide_body if source.wide else f"payload-{n}"
            lines.append(f"{source.source_id}-{n:03d},{body}")
        if source is CORRUPT:
            # A truncated write from the upstream export: the second record has no body.
            lines[1] = f"{source.source_id}-002"
        (directory / f"{source.source_id}.csv").write_text("\n".join(lines) + "\n")


def read_source(source_id: str) -> str:
    return (PATHS["sources"] / f"{source_id}.csv").read_text()


def parse(source_id: str, text: str) -> list[Row]:
    """Split raw source text into rows, raising on a malformed record."""
    rows = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        record_id, sep, body = line.partition(",")
        if not sep or not body:
            raise ValueError(f"{source_id}: malformed record on line {lineno}: {line[:32]!r}")
        rows.append(Row(source_id=source_id, record_id=record_id, body=body))
    return rows


def seed_warehouse(path: Path) -> None:
    """Create the destination table.

    ``load_key`` is where idempotency lives: unique when set, and the keyed loader sets it
    from ``ctx.idempotency_key``. A loader that leaves it ``NULL`` can insert the same
    record as many times as it is retried, which is exactly what section 2 shows.
    """
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            DROP TABLE IF EXISTS warehouse;
            CREATE TABLE warehouse (
                insert_id INTEGER PRIMARY KEY AUTOINCREMENT,
                load_key  TEXT,
                source_id TEXT NOT NULL,
                record_id TEXT NOT NULL,
                body      TEXT NOT NULL
            );
            CREATE UNIQUE INDEX warehouse_load_key
                ON warehouse(load_key) WHERE load_key IS NOT NULL;
            """
        )
        conn.commit()
    finally:
        conn.close()


def warehouse_counts() -> dict[str, tuple[int, int]]:
    """Per source: rows in the warehouse, and how many distinct records they cover."""
    conn = sqlite3.connect(PATHS["warehouse"])
    try:
        rows = conn.execute(
            "SELECT source_id, COUNT(*), COUNT(DISTINCT record_id) "
            "FROM warehouse GROUP BY source_id"
        ).fetchall()
    finally:
        conn.close()
    return {source_id: (total, distinct) for source_id, total, distinct in rows}


# -- the pipeline ----------------------------------------------------------------------


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


@satay.task(retries=1, side_effect=True, idempotent=True)
async def load(batch: Batch) -> LoadReport:
    """Write one source's rows to the warehouse, exactly once per logical call.

    ``side_effect=True`` says this task touches the outside world; ``idempotent=True`` is
    the promise that it keys that effect on ``ctx.idempotency_key`` — which is what the
    ``INSERT OR IGNORE`` below actually does. Without the promise, ``effect_safety=strict``
    refuses to schedule a retryable side-effecting task at all.

    The key is ``sha256(run_id, task_name, map_key)``: **stable across the retries of one
    logical call**, distinct across sources and runs. So a second attempt after a lost ack
    re-derives the same key and its inserts are ignored.
    """
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

    print(
        f"     load {batch.source_id:<13} attempt {ctx.attempt}: "
        f"{written} row(s) written, {deduped} already keyed in (ignored)"
    )

    if PHASE["n"] == 1 and batch.source_id == LOST_ACK_SOURCE and ctx.attempt == 1:
        # The classic ambiguous completion: the warehouse committed, the ack did not
        # arrive. Satay cannot know the write landed, so it retries — at-least-once.
        raise ConnectionError("warehouse committed the rows but the ack never came back")

    if PHASE["n"] == 1 and batch.source_id == CRASH_AFTER_LOADING:
        # Arm the crash here, not at ``start``, so the worker dies mid-*load* instead of
        # mid-extract: the very next committed ``TaskCompleted`` is this call's own.
        INJECTOR.crash_after("TaskCompleted")

    return LoadReport(
        source_id=batch.source_id, rows_written=written, rows_deduped=deduped, attempts=ctx.attempt
    )


@satay.workflow
async def elt_pipeline(sources: list[Source]) -> PipelineReport:
    """Extract every source, transform each, load each — three keyed fan-outs.

    ``concurrency=1`` only to make the crash point deterministic for a demo; a real load
    leaves it at the default bound of 8 and still rejoins results in input order.
    """
    raw = await satay.map(extract, sources, key=source_key, concurrency=1)
    batches = await satay.map(transform, raw, key=extracted_key, concurrency=1)
    reports = await satay.map(load, batches, key=batch_key, concurrency=1)
    widest = max(raw, key=lambda item: len(item.text))
    return PipelineReport(reports=reports, widest_sample=widest.text)


# -- the same load, without the key ------------------------------------------------------


@satay.task(retries=1, side_effect=True)
async def load_carelessly(batch: Batch) -> int:
    """The loader everybody writes first: a plain INSERT that ignores the idempotency key.

    Note what it does *not* declare: ``idempotent=True``. Satay knows this is unsafe and
    says so — ``effect_safety=strict`` would refuse to schedule it, and the default
    ``warn`` logs it. This run sets ``warn`` on purpose so you can see the damage.
    """
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
    print(f"     careless load attempt {ctx.attempt}: {len(batch.rows)} rows INSERTed")
    if ctx.attempt == 1:
        raise ConnectionError("warehouse committed the rows but the ack never came back")
    return len(batch.rows)


@satay.workflow
async def careless_load(batch: Batch) -> int:
    """One load, one lost ack, one retry — and no idempotency key to save it."""
    return await load_carelessly(batch)


# -- one bad source: fail-fast, and the workaround ---------------------------------------


@satay.task()
async def extract_strictly(source: Source) -> Extracted:
    """Extract and validate. A corrupt source raises, which fails the whole ``map``."""
    record("extract_strictly", source.source_id)
    text = read_source(source.source_id)
    parse(source.source_id, text)  # validate now; raises on a malformed record
    return Extracted(source_id=source.source_id, text=text)


@satay.workflow
async def strict_extract(sources: list[Source]) -> int:
    """Fan out over sources where one is corrupt. Fail-fast: the ``map`` raises (ADR-0020)."""
    extracted = await satay.map(extract_strictly, sources, key=source_key)
    return sum(len(item.text) for item in extracted)


@satay.task()
async def extract_outcome(source: Source) -> Outcome:
    """The same extract, but it never raises — it reports.

    This is the whole workaround for fail-fast fan-out today: catch inside the task and
    return a union you define yourself, so the ``map`` always succeeds and the workflow
    partitions the results by hand.
    """
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


# -- plumbing ----------------------------------------------------------------------------


def resolve_workdir() -> tuple[Path, bool]:
    """Where this run's journal lives, and whether it outlives the process.

    An explicit argument or ``SATAY_DATA_DIR`` means the caller wants the journal kept
    (so Studio can open it); with neither, fall back to a throwaway temp directory so the
    file stays self-contained wherever it is downloaded and run.
    """
    override = sys.argv[1] if len(sys.argv) > 1 else os.environ.get(DATA_DIR_ENV_VAR)
    if override:
        workdir = Path(override).expanduser().resolve()
        workdir.mkdir(parents=True, exist_ok=True)
        return workdir, True
    return Path(tempfile.mkdtemp(prefix="satay-elt-")), False


async def drive(factory: Any, clock: ManualClock, *, step: int = 61) -> Any:
    """Await ``factory()``, advancing ``clock`` through every retry backoff.

    Backoff sleeps go through the injected clock, so under a ``ManualClock`` nothing moves
    until someone advances it. Same loop as ``drain`` in ``tests/conftest.py``.
    """
    task = asyncio.ensure_future(factory())
    try:
        for _ in range(500):
            for _ in range(4):
                await asyncio.sleep(0)  # let the drive reach its next suspension point
            if task.done():
                return await task
            if clock.pending_sleepers:
                clock.advance(step)
    finally:
        if not task.done():
            task.cancel()
    raise RuntimeError("the run never settled — is something waiting on real time?")


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def raw_journal_row(database: Path, task_name: str, key: str) -> str:
    """The literal ``payload_json`` the journal holds for one keyed ``TaskCompleted``.

    Read with plain ``sqlite3`` on purpose: going through the store would rehydrate the
    blob and hide the very thing this is here to show.
    """
    conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT payload_json FROM events WHERE type = ? ORDER BY seq",
            (EventType.TASK_COMPLETED.value,),
        ).fetchall()
    finally:
        conn.close()
    needle_task, needle_key = f'"task_name":"{task_name}"', f'"key":"{key}"'
    for (payload_json,) in rows:
        if needle_task in payload_json and needle_key in payload_json:
            return str(payload_json)
    raise AssertionError(f"no TaskCompleted row for {task_name}/{key}")


async def recorded_output(store: SQLiteStore, run_id: str, task_name: str, key: str) -> Any:
    """The same event, read back **through Satay** — blob rehydrated, spill invisible.

    The store decodes the payload without a return annotation to guide it, so a recorded
    dataclass comes back as its field dict; the *values* are the whole point here.
    """
    for event in await store.read_events(run_id):
        if event.type is not EventType.TASK_COMPLETED:
            continue
        if event.payload.get("task_name") == task_name and event.payload.get("key") == key:
            return event.payload["output_ref"]
    raise AssertionError(f"no TaskCompleted event for {task_name}/{key}")


# -- the story ---------------------------------------------------------------------------


async def section_1_and_2(store: SQLiteStore, clock: ManualClock) -> tuple[str, PipelineReport]:
    """Run the pipeline through a mid-load crash, then show the unkeyed loader's damage."""
    print("1) extract → transform → load, with the worker dying mid-load")
    print("   phase 1: the loader arms a crash the moment `orders` commits")
    PHASE["n"] = 1
    handle = satay.start(
        elt_pipeline, SOURCES, store=store, clock=clock, rng=SeededRng(7), injector=INJECTOR
    )
    run_id = handle.run_id
    try:
        await drive(handle.result, clock)
    except SimulatedCrash as exc:
        print(f"     worker died: {exc}")

    loaded = [
        event.payload["key"]
        for event in await store.read_events(run_id)
        if event.type is EventType.TASK_COMPLETED and event.payload.get("task_name") == "load"
    ]
    print(f"     durably loaded before the crash: {loaded}")

    print("   phase 2: resume the same run — only the unresolved sources run again")
    PHASE["n"] = 2
    resumed = satay.start(elt_pipeline, SOURCES, run_id=run_id, store=store, clock=clock)
    report: PipelineReport = await drive(resumed.result, clock)
    print(f"     status: {await resumed.status()}   run {run_id}\n")

    counts = warehouse_counts()
    print("   per-source ledger")
    header = f"     {'source':<14} {'extract':<9} {'transform':<11} {'load':<18} warehouse"
    print(header)
    for source in SOURCES:
        sid = source.source_id
        load_phase = RAN_IN_PHASE[("load", sid)]
        verdict = f"phase {load_phase}" + (" (REUSED)" if load_phase == 1 else "")
        total, distinct = counts[sid]
        print(
            f"     {sid:<14} phase {RAN_IN_PHASE[('extract', sid)]:<3} "
            f"phase {RAN_IN_PHASE[('transform', sid)]:<5} {verdict:<18} "
            f"{total} row(s), {distinct} distinct"
        )

    calls = 3 * len(SOURCES)
    bodies = sum(EXECUTIONS.values())
    print(f"\n     {calls} durable calls, {bodies} task bodies executed, 1 crash, 1 lost ack.")
    print("     The one extra body is `orders`' load retry — at-least-once, by design.")
    print("     Every source holds exactly as many warehouse rows as it has records.\n")

    # -- 2 ------------------------------------------------------------------------------
    print("2) what the idempotency key is actually for")
    print("   the same lost ack, loaded by a task that ignores ctx.idempotency_key:")
    PHASE["n"] = 3
    careless_rows = parse("orders-careless", read_source("orders"))
    careless = satay.start(
        careless_load,
        Batch(source_id="orders-careless", rows=careless_rows),
        store=store,
        clock=clock,
        rng=SeededRng(7),
        effect_safety="warn",  # `strict` would refuse to schedule it at all
    )
    await drive(careless.result, clock)
    counts = warehouse_counts()
    bad_total, bad_distinct = counts["orders-careless"]
    good_total, good_distinct = counts[LOST_ACK_SOURCE]
    print(
        f"     unkeyed loader: {bad_total} rows for {bad_distinct} records"
        f"  ← every record duplicated"
    )
    print(
        f"     keyed loader:   {good_total} rows for {good_distinct} records"
        f"  ← same lost ack, no damage"
    )
    print("     Satay guarantees at-least-once. The key is the half you have to write.")
    print("     `effect_safety=strict` refuses to even schedule the unkeyed one.")
    print("     One caveat worth knowing: ctx.idempotency_key is sha256(run_id, task, key),")
    print("     so it dedupes retries and resumes *of one run* — not a second run over the")
    print("     same data. For that, give the run itself a key: satay.start(..., ")
    print("     idempotency_key='nightly-2026-01-01'), which resolves to the same run.\n")
    return run_id, report


async def section_3(workdir: Path, store: SQLiteStore, run_id: str, report: PipelineReport) -> None:
    """Show the journal holding a reference where the value is too wide to inline."""
    print("3) blob spill — one source is too wide for a journal row")
    wide = next(source for source in SOURCES if source.wide)
    row = raw_journal_row(db_path(workdir), "extract", wide.source_id)
    through_satay = await recorded_output(store, run_id, "extract", wide.source_id)
    blobs = sorted(blob_dir(workdir).glob("*.blob"))
    # The row names its own blob: the id in the reference is the content address, so the
    # file it points at is found by name, never by guessing which blob is the big one.
    blob_id = str(json.loads(row)["output_ref"]["id"])
    referenced = blob_dir(workdir) / f"{blob_id}.blob"

    recorded_text = str(through_satay["text"])
    print(
        f"     {wide.source_id} extracted {len(recorded_text):,} characters; "
        f"the spill threshold is {SPILL_THRESHOLD_BYTES:,} bytes"
    )
    print(f"     journal row  : {row}")
    print(f"                    ({len(row)} bytes on the row — a reference, not the value)")
    print(
        f"     blob on disk : blobs/{referenced.name[:12]}….blob  "
        f"({referenced.stat().st_size:,} bytes, sha256-addressed)"
    )
    print(f"     store.read_events()  → {len(recorded_text):,} chars, sha {sha(recorded_text)}")
    print(
        f"     handle.result()      → {len(report.widest_sample):,} chars, "
        f"sha {sha(report.widest_sample)}"
    )
    print(f"     {len(blobs)} blob(s) written in total; the workflow body never mentions blobs.")
    print(
        "     The resumed run in section 1 read this source's value back out of the blob\n"
        "     to build its report — spill survives a crash as transparently as it is written.\n"
    )


async def section_4_and_5(store: SQLiteStore, clock: ManualClock) -> None:
    """One corrupt source: fail-fast, what it costs, and the only workaround there is."""
    sources = [*SOURCES, CORRUPT]
    print("4) one source fails — fan-out is fail-fast (ADR-0020)")
    PHASE["n"] = 4
    doomed = satay.start(strict_extract, sources, store=store, clock=clock)
    try:
        await drive(doomed.result, clock)
    except satay.WorkflowFailedError as exc:
        print(f"     the map raised {exc.error_type}: {exc.error_message}")
    print(f"     status: {await doomed.status()}   run {doomed.run_id}")

    events = list(await store.read_events(doomed.run_id))
    survivors = [
        event.payload["key"]
        for event in events
        if event.type is EventType.TASK_COMPLETED
        and event.payload.get("task_name") == "extract_strictly"
    ]
    discarded = sum(len(read_source(key)) for key in survivors)
    print(f"     sibling extracts that COMPLETED anyway: {sorted(survivors)}")
    print(f"     …and are now unreachable: {discarded:,} characters of finished work,")
    print("       including the 300 KB source, thrown away because one sibling raised.")
    print("     The results are on the journal, but the run is terminal: `satay.start(")
    print("       run_id=…)` on a failed run re-raises rather than resuming. Forking the")
    print("       run is the only way back in, and there is no `return_exceptions` mode.")
    tally: dict[str, int] = {}
    for event in events:
        tally[event.type.value] = tally.get(event.type.value, 0) + 1
    print(f"     journal: {tally}")
    print("     Read that tally again: the successes are recorded. Only the workflow lost.")

    print("\n5) the workaround: return an outcome instead of raising")
    PHASE["n"] = 5
    resilient = satay.start(resilient_extract, sources, store=store, clock=clock)
    outcomes: list[Outcome] = await drive(resilient.result, clock)
    good = [outcome for outcome in outcomes if outcome.ok]
    bad = [outcome for outcome in outcomes if not outcome.ok]
    print(f"     status: {await resilient.status()}   run {resilient.run_id}")
    print(f"     extracted: {[outcome.source_id for outcome in good]}")
    for outcome in bad:
        print(f"     quarantined: {outcome.source_id} — {outcome.error}")
    print(f"     {len(good)}/{len(outcomes)} sources survived, and the pipeline can go on.")
    print("     The cost: you hand-roll the union type, the try/except and the partition,")
    print("     and you give up ever seeing the failure as a failure on the journal.\n")


async def main() -> None:
    workdir, durable = resolve_workdir()
    PATHS["sources"] = workdir / "sources"
    PATHS["warehouse"] = workdir / "warehouse.db"
    seed_sources(PATHS["sources"])
    seed_warehouse(PATHS["warehouse"])

    store = SQLiteStore.open(db_path(workdir))
    clock = ManualClock()

    print("Satay — an ELT pipeline: fan-out extract, idempotent load, blob spill")
    print(f"data dir:  {workdir}")
    print(f"sources:   {[source.source_id for source in SOURCES]} (files in {PATHS['sources']})")
    print(f"warehouse: {PATHS['warehouse']}\n")

    run_id, report = await section_1_and_2(store, clock)
    await section_3(workdir, store, run_id, report)
    await section_4_and_5(store, clock)

    total = sum(count for count, _ in warehouse_counts().values())
    print(f"warehouse holds {total} rows across {len(warehouse_counts())} source ids.")
    store.close()

    if durable:
        print(f"\njournal kept in {workdir}")
        print(f"open the pipeline in Studio:  satay dev --data-dir {workdir}")
        print(f"or as text:                   satay runs show {run_id} --data-dir {workdir}")
    else:
        print(
            f"\njournal went to a temp dir ({workdir}) and is not worth keeping.\n"
            f"Re-run with SATAY_DATA_DIR set to browse it in Studio."
        )


if __name__ == "__main__":
    asyncio.run(main())
