"""SLICE V1 crash-recovery demo (build-plan step 13).

Runs the two-task ``demo(value)`` workflow against a **durable on-disk** SQLite store,
kills the worker right after ``step_one``'s ``TaskCompleted`` commits, then restarts
and resumes the same ``run_id``. The file-backed execution-count marker proves
``step_one`` is *reused* (its count stays at 1) while ``step_two`` executes, and the
final result is correct. Finally it prints the timeline the way ``satay runs show``
does, with the ⚡ interruption marker at ``WorkflowResumed``.

    uv run python examples/crash_recovery_demo.py        # throwaway temp data dir
    SATAY_DATA_DIR=.satay-demo uv run python examples/crash_recovery_demo.py
    make demo                                           # the above, then Satay Studio

By default the run lands in a throwaway temp directory, so the file is self-contained and
leaves nothing behind wherever you curl it to. Set ``SATAY_DATA_DIR`` (or pass a path as
the first argument) to keep the journal somewhere durable — ``satay dev --data-dir <that
path>`` can then open the run in Studio, which is what ``make demo`` wires up.

The two phases below are deliberately independent — in a real deployment phase 1 is a
worker that dies and phase 2 is a fresh process reading the same ``./.satay`` — but the
durability proof is identical whether the "restart" crosses a process boundary or not.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

from satay import demo
from satay.api.primitives import start
from satay.config import DATA_DIR_ENV_VAR, db_path
from satay.journal.store import SQLiteStore
from satay.journal.timeline import render_timeline
from satay.testing.faults import FaultInjector, SimulatedCrash


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
    return Path(tempfile.mkdtemp(prefix="satay-demo-")), False


async def main() -> None:
    workdir, durable = resolve_workdir()
    os.environ[demo.MARKER_ENV_VAR] = str(workdir / "marker.json")
    demo.reset_executions()
    database = db_path(workdir)

    # -- Phase 1: worker runs, then dies right after step_one's TaskCompleted. --------
    store = SQLiteStore.open(database)
    injector = FaultInjector()
    injector.crash_after("TaskCompleted")
    handle = start(demo.demo, 1, store=store, injector=injector)
    run_id = handle.run_id
    print(f"phase 1: starting run {run_id}")
    try:
        await handle.result()
    except SimulatedCrash as exc:
        print(f"phase 1: worker crashed — {exc}")
    store.close()
    print(f"phase 1: step_one executions so far = {demo.execution_count('step_one')}")
    print(f"phase 1: step_two executions so far = {demo.execution_count('step_two')}\n")

    # -- Phase 2: a fresh worker opens the same DB and resumes the same run. -----------
    store = SQLiteStore.open(database)
    print(f"phase 2: resuming run {run_id}")
    resumed = start(demo.demo, 1, run_id=run_id, store=store)
    result = await resumed.result()
    print(f"phase 2: final result = {result} (expected 4)")
    print(f"phase 2: step_one executions = {demo.execution_count('step_one')} (REUSED, still 1)")
    print(f"phase 2: step_two executions = {demo.execution_count('step_two')} (ran once)\n")

    events = list(await store.read_events(run_id))
    store.close()
    print(render_timeline(events, run_id=run_id))

    if durable:
        print(f"\njournal kept in {workdir}")
        print(f"open it in Satay Studio:  satay dev --data-dir {workdir}")
        print(f"or as text:               satay runs show {run_id} --data-dir {workdir}")
    else:
        print(
            f"\njournal was written to a temp dir ({workdir}) and is not worth keeping.\n"
            f"Re-run with SATAY_DATA_DIR set (or `make demo`) to browse it in Studio."
        )

    del os.environ[demo.MARKER_ENV_VAR]


if __name__ == "__main__":
    asyncio.run(main())
