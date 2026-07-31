"""Retries with capped exponential backoff, read back off the journal.

A task that **fails twice and succeeds on the third attempt**, plus a task that
exhausts its retries and fails the run. Every attempt is a journal event, so the retry
schedule is not a log line you have to trust — it is durable state you can read back
(and browse in Studio).

    uv run python examples/retries_backoff_demo.py        # throwaway temp data dir
    SATAY_DATA_DIR=.satay-demo uv run python examples/retries_backoff_demo.py

Why there is a ``ManualClock`` in a demo: backoff waits go through the *injected* clock
(``base * 2**(failure-1)``, full-jitter, capped at 60s — ADR-0006), so swapping in
``satay.testing.ManualClock`` replays a real retry schedule in **zero wall-clock time**.
The recorded ``next_delay`` values are the real ones; nobody sat and waited for them.
Somebody still has to move that clock, and that somebody ships with Satay:
``satay.testing.settle`` drives an awaitable and advances the clock through every wait it
suspends on. It is the same helper your own tests want (the ``drain`` fixture in
``satay.testing.fixtures`` hands you this exact function).

By default the run lands in a throwaway temp directory, so this file is self-contained
wherever you download it. Set ``SATAY_DATA_DIR`` (or pass a path as the first argument)
to keep the journal, then ``satay dev --data-dir <that path>`` opens it in Studio.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

import satay
from satay.config import DATA_DIR_ENV_VAR, db_path
from satay.journal.events import Event, EventType
from satay.journal.store import SQLiteStore
from satay.journal.timeline import render_timeline
from satay.testing import ManualClock, SeededRng, settle

#: Physical executions per task name, bumped on *real* execution. Makes "the body ran
#: three times" observable rather than a claim (ADR-0011).
EXECUTIONS: dict[str, int] = {}

#: The attempt on which the flaky fetch stops timing out. ``retries=2`` allows exactly
#: three physical attempts, so this is the last one that can still succeed.
SUCCEEDS_ON_ATTEMPT = 3

#: Seed for the backoff jitter, so the delays below are reproducible run to run.
JITTER_SEED = 1234


def record(name: str) -> None:
    EXECUTIONS[name] = EXECUTIONS.get(name, 0) + 1


@satay.task(retries=2)
async def fetch_rate(pair: str) -> float:
    """A flaky read: the upstream times out twice, then answers (three attempts).

    ``retries=2`` drives the executor's retry loop. The failure decision reads
    ``ctx.attempt`` off the task context, so the schedule is identical every run.

    This task is a read, which is why it declares no ``side_effect=``. A *retryable*
    side-effecting task has to promise ``idempotent=True`` (that it keys its effect on
    ``ctx.idempotency_key``) or ``effect_safety=strict`` rejects it at schedule time.
    """
    record("fetch_rate")
    ctx = satay.task_context()
    if ctx.attempt < SUCCEEDS_ON_ATTEMPT:
        raise RuntimeError(f"upstream rate API timed out (attempt {ctx.attempt})")
    return 1.35


@satay.task()
async def convert(amount: float, rate: float) -> float:
    """Apply the fetched rate. Runs once — the retries upstream are invisible here."""
    record("convert")
    return round(amount * rate, 2)


@satay.workflow
async def quote(amount: float) -> float:
    """Fetch a rate (retried) and convert an amount with it."""
    rate = await fetch_rate("USD/SGD")
    return await convert(amount, rate)


@satay.task(retries=1)
async def fetch_from_dead_host(pair: str) -> float:
    """A read that never succeeds: two attempts, then the run fails terminally."""
    record("fetch_from_dead_host")
    raise ConnectionError(f"no route to rates.invalid while fetching {pair}")


@satay.workflow
async def doomed_quote(amount: float) -> float:
    """A workflow whose only task exhausts its retries."""
    return await fetch_from_dead_host("USD/SGD")


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
    return Path(tempfile.mkdtemp(prefix="satay-retries-")), False


def print_attempts(events: list[Event], task_name: str) -> float:
    """Print one line per recorded attempt of ``task_name``; return the total backoff."""
    total_delay = 0.0
    attempt = 0
    for event in events:
        payload = event.payload
        if payload.get("task_name") != task_name:
            continue
        if event.type is EventType.TASK_ATTEMPT_STARTED:
            # ``TaskCompleted`` carries no attempt number, so the winning attempt is the
            # last ``TaskAttemptStarted`` before it.
            attempt = payload["attempt"]
        elif event.type is EventType.TASK_ATTEMPT_FAILED:
            error = payload.get("error", {})
            delay = payload.get("next_delay")
            waited = f"backoff {delay:.3f}s" if delay is not None else "no retry left"
            total_delay += delay or 0.0
            print(
                f"  attempt {payload['attempt']}  FAILED   "
                f"{error.get('type')}: {error.get('message')}  ({waited})"
            )
        elif event.type is EventType.TASK_COMPLETED:
            print(f"  attempt {attempt}  SUCCEEDED")
    return total_delay


async def main() -> None:
    workdir, durable = resolve_workdir()
    store = SQLiteStore.open(db_path(workdir))
    clock = ManualClock()

    print("Satay — retries and capped exponential backoff")
    print(f"data dir: {workdir}\n")

    # -- 1: fails twice, succeeds on the third attempt --------------------------------
    handle = satay.start(quote, 100.0, store=store, clock=clock, rng=SeededRng(JITTER_SEED))
    print(f"1) fetch_rate fails twice then succeeds — run {handle.run_id}")
    # ``settle`` advances the clock in coarse 61s steps (one step clears the 60s backoff
    # cap), which is why the journal timestamps below jump a minute at a time while the
    # *recorded* delays are sub-second. Virtual time is free; precision buys nothing.
    result = await settle(handle.result, clock)

    events = list(await store.read_events(handle.run_id))
    backoff = print_attempts(events, "fetch_rate")
    print(f"  result: {result} SGD   status: {await handle.status()}")
    print(f"  fetch_rate bodies executed: {EXECUTIONS['fetch_rate']} (at-least-once, by design)")
    print(f"  convert bodies executed:    {EXECUTIONS['convert']} (the retries never reach it)")
    print(f"  backoff scheduled: {backoff:.3f}s in total — none of it real time\n")
    print(render_timeline(events, run_id=handle.run_id))

    # -- 2: retries run out and the run fails terminally ------------------------------
    doomed = satay.start(doomed_quote, 100.0, store=store, clock=clock, rng=SeededRng(JITTER_SEED))
    print(f"\n2) fetch_from_dead_host exhausts retries=1 — run {doomed.run_id}")
    try:
        await settle(doomed.result, clock)
    except satay.WorkflowFailedError as exc:
        print(f"  raised {exc.error_type}: {exc.error_message}")
    print(f"  status: {await doomed.status()}")
    print(f"  attempts made: {EXECUTIONS['fetch_from_dead_host']} (retries=1 → 1 + 1 retry)")
    print("  the last error is what the run fails with; earlier attempts stay on the journal\n")
    failed_events = list(await store.read_events(doomed.run_id))
    print_attempts(failed_events, "fetch_from_dead_host")

    store.close()

    if durable:
        print(f"\njournal kept in {workdir}")
        print(f"browse both runs:  satay dev --data-dir {workdir}")
        print(f"or as text:        satay runs show {handle.run_id} --data-dir {workdir}")
    else:
        print(
            f"\njournal went to a temp dir ({workdir}) and is not worth keeping.\n"
            f"Re-run with SATAY_DATA_DIR set to browse it in Studio."
        )


if __name__ == "__main__":
    asyncio.run(main())
