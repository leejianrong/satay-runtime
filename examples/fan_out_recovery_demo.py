"""THE signature demo: crash mid-fan-out, restart, and only the unfinished items re-run.

``satay.map`` fans a task out over items, and every item is a **keyed durable call** —
``(task_name, key(item))``. That key is the whole trick: on restart each item consults
the journal for itself, so an item that already committed a result is *reused* and only
the unresolved ones execute again. Five documents, two crashes, five executions total.

    uv run python examples/fan_out_recovery_demo.py        # throwaway temp data dir
    SATAY_DATA_DIR=.satay-demo uv run python examples/fan_out_recovery_demo.py

Phase 1 indexes one document and the worker dies. Phase 2 restarts, reuses that document,
indexes one more, and dies again. Phase 3 restarts and finishes. The table at the end
shows, per document, which phase actually ran it and whether the final restart reused it
— and the total execution count proves nothing was indexed twice.

The "crash" is ``satay.testing.FaultInjector``: a real, durable interruption raised
immediately after a chosen journal event commits. Nothing is mocked and nothing is
rolled back — the journal is exactly as truncated as a ``kill -9`` would leave it.

By default the run lands in a throwaway temp directory, so this file is self-contained
wherever you download it. Set ``SATAY_DATA_DIR`` (or pass a path as the first argument)
to keep the journal, then ``satay dev --data-dir <that path>`` opens it in Studio.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import satay
from satay.config import DATA_DIR_ENV_VAR, db_path
from satay.journal.events import Event, EventType
from satay.journal.store import SQLiteStore
from satay.journal.timeline import render_timeline
from satay.testing import FaultInjector, SimulatedCrash


@dataclass(frozen=True)
class Document:
    """One fan-out item. Dataclasses round-trip through the journal codec as-is."""

    doc_id: str
    pages: int


#: The batch to index. Order is preserved in the result no matter what completes when.
BATCH = [
    Document("doc-intro", 3),
    Document("doc-methods", 11),
    Document("doc-results", 7),
    Document("doc-discussion", 9),
    Document("doc-appendix", 2),
]

#: Physical executions per document key — the reuse-vs-re-run marker (ADR-0011).
EXECUTIONS: dict[str, int] = {}

#: Which phase each document was *actually indexed* in, recorded on real execution.
INDEXED_IN_PHASE: dict[str, int] = {}

#: The phase currently running (1, 2 or 3), so the task can stamp its executions.
PHASE = {"n": 0}


def document_key(doc: Document) -> str:
    """The stable fan-out identity of one item.

    ``key=`` is required by ``satay.map`` and must be unique, stable and non-empty per
    item (ADR-0002). Stable across restarts is what makes an item reusable: derive it
    from the item's own identity, never from a counter, a hash of a mutable field, or
    the position in the list.
    """
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
    """Fan out ``index_document`` over the batch, keyed per document.

    ``concurrency=1`` here only to make the crash point deterministic for a demo (exactly
    the items whose ``TaskCompleted`` committed survive). Real fan-outs leave it alone and
    get the default bound of 8 in-flight items; results still rejoin in **input order**.
    """
    return await satay.map(index_document, docs, key=document_key, concurrency=1)


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
    return Path(tempfile.mkdtemp(prefix="satay-fanout-")), False


def completed_keys(events: list[Event]) -> list[str]:
    """The fan-out key of every item whose result is durably on the journal."""
    return [
        event.payload["key"]
        for event in events
        if event.type is EventType.TASK_COMPLETED and "key" in event.payload
    ]


async def crash_once_indexing(
    store: SQLiteStore, run_id: str | None, phase: int
) -> tuple[str, list[str]]:
    """Drive ``index_batch`` until the worker dies after the next item commits.

    Passing ``run_id=None`` starts a new run; passing an existing one resumes it. The
    injector raises after the next ``TaskCompleted`` commit, which is a worker death
    *after* durable state was written — the hardest case, and the one that has to work.
    """
    PHASE["n"] = phase
    injector = FaultInjector()
    injector.crash_after("TaskCompleted")
    handle = satay.start(index_batch, BATCH, run_id=run_id, store=store, injector=injector)
    try:
        await handle.result()
    except SimulatedCrash as exc:
        print(f"  worker died: {exc}")
    done = completed_keys(list(await store.read_events(handle.run_id)))
    print(f"  durably indexed so far: {done}")
    return handle.run_id, done


async def main() -> None:
    workdir, durable = resolve_workdir()
    store = SQLiteStore.open(db_path(workdir))

    print("Satay — fan-out with crash recovery")
    print(f"data dir: {workdir}")
    print(f"batch: {[d.doc_id for d in BATCH]}\n")

    # -- Phase 1: a fresh run, killed after the first item commits. --------------------
    print("phase 1: start the fan-out, kill the worker after the first item")
    run_id, after_first = await crash_once_indexing(store, None, phase=1)
    print(f"  run {run_id}\n")

    # -- Phase 2: restart, reuse what survived, then die again. ------------------------
    print("phase 2: restart the same run — then kill it again after one more item")
    _, after_second = await crash_once_indexing(store, run_id, phase=2)
    print(f"  reused from the journal (never re-indexed): {after_first}")
    print(f"  newly indexed in phase 2: {sorted(set(after_second) - set(after_first))}\n")

    # -- Phase 3: restart clean and finish. -------------------------------------------
    print("phase 3: restart with no fault — the run finishes")
    PHASE["n"] = 3
    resumed = satay.start(index_batch, BATCH, run_id=run_id, store=store)
    results = await resumed.result()
    print(f"  result: {results}")
    print(f"  status: {await resumed.status()}")
    print("  results rejoin in INPUT order, not completion order\n")

    # -- What was reused, and what re-ran. --------------------------------------------
    print("per-document ledger")
    print(f"  {'document':<16} {'indexed in':<12} {'executions':<11} on the final restart")
    for doc in BATCH:
        key = document_key(doc)
        phase = INDEXED_IN_PHASE[key]
        verdict = "REUSED from the journal" if phase < 3 else "ran now"
        print(f"  {key:<16} phase {phase:<6} {EXECUTIONS[key]:<11} {verdict}")

    total = sum(EXECUTIONS.values())
    print(f"\n  {len(BATCH)} documents, 2 crashes, {total} executions in total.")
    print("  Every document was indexed exactly once. That is the guarantee.")

    events = list(await store.read_events(run_id))
    keys = completed_keys(events)
    print(f"  TaskCompleted on the journal: {len(keys)} — one per key, {len(set(keys))} distinct.")
    resumes = sum(1 for e in events if e.type is EventType.WORKFLOW_RESUMED)
    print(f"  WorkflowResumed events: {resumes} — the two ⚡ markers below.\n")
    print(render_timeline(events, run_id=run_id))
    store.close()

    if durable:
        print(f"\njournal kept in {workdir}")
        print(f"open the fan-out in Studio:  satay dev --data-dir {workdir}")
        print(f"or as text:                  satay runs show {run_id} --data-dir {workdir}")
    else:
        print(
            f"\njournal went to a temp dir ({workdir}) and is not worth keeping.\n"
            f"Re-run with SATAY_DATA_DIR set to browse it in Studio."
        )


if __name__ == "__main__":
    asyncio.run(main())
