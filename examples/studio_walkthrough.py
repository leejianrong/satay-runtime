"""Produce one deliberately interesting run, then walk you into Satay Studio.

The other examples each show one primitive. This one builds a single run that touches
nearly all of them — a keyed fan-out with a retried item, a crash and resume (⚡), a
durable sleep, an external event, a linked child workflow, and self-reported model usage
— plus a second run that fails outright, so the run list has both outcomes to compare.
Then it prints exactly what to click.

    SATAY_DATA_DIR=.satay-demo uv run python examples/studio_walkthrough.py
    uv run --extra studio satay dev --data-dir .satay-demo

Studio ships in the optional ``satay[studio]`` extra; producing the run needs only the
core. Without ``SATAY_DATA_DIR`` (or a path as the first argument) the journal goes to a
throwaway temp dir — fine for a look, but pass a real directory if you want to browse it.

Everything below runs in one process with a ``ManualClock``, so the eight-hour sleep and
the event wait resolve instantly. ``satay dev`` runs the same worker loop against a real
clock, which is why the finished journal is browsable by it either way.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import satay
from satay.api.run_handle import WorkflowFailedError
from satay.config import DATA_DIR_ENV_VAR, db_path
from satay.control.security import TOKEN_HEADER
from satay.journal.events import Event, EventType
from satay.journal.store import SQLiteStore
from satay.journal.timeline import interruption_seqs, model_usage, render_timeline
from satay.testing import FaultInjector, ManualClock, SimulatedCrash
from satay.timers import TimerEventWorker

#: Physical executions per task name, so reuse across the crash is observable.
EXECUTIONS: dict[str, int] = {}

#: The feeds the digest fans out over.
SOURCES = ["hn", "lobsters", "arxiv", "changelog"]

#: The feed that times out on its first attempt, to put a retry on the timeline.
FLAKY_SOURCE = "arxiv"

#: The digest's topic (the workflow input).
TOPIC = "durable execution"

#: The event key the digest waits on before publishing (kept short and boring on
#: purpose — a high-entropy string next to a name ending in ``_KEY`` trips secret
#: scanners, and this is an event correlation key, not a credential).
APPROVAL_KEY = "digest-1"

#: The session-token header, canonically cased for display (HTTP headers are
#: case-insensitive; the runtime compares the lowercase form in ``TOKEN_HEADER``).
TOKEN_HEADER_DISPLAY = TOKEN_HEADER.title()

#: The port ``satay dev`` binds by default. Mirrors
#: ``satay.devstack.orchestrator.DEFAULT_PORT``, which is not importable from here: that
#: module loads FastAPI/uvicorn, and this example only needs the core.
STUDIO_DEFAULT_PORT = 8787


def record(name: str) -> None:
    EXECUTIONS[name] = EXECUTIONS.get(name, 0) + 1


@dataclass(frozen=True)
class PublishApproval:
    """The external go/no-go delivered with ``satay.send_event``."""

    approved: bool
    reviewer: str


def source_key(source: str) -> str:
    """The stable fan-out key for one feed (ADR-0002: unique, stable, non-empty)."""
    return f"source-{source}"


@satay.task(retries=2)
async def fetch_feed(source: str) -> dict[str, object]:
    """Fetch one feed. ``arxiv`` times out on its first attempt, then answers."""
    record(f"fetch:{source}")
    ctx = satay.task_context()
    if source == FLAKY_SOURCE and ctx.attempt == 1:
        raise TimeoutError(f"{source} feed timed out on attempt {ctx.attempt}")
    return {"source": source, "items": len(source) * 3}


@satay.task()
async def summarize(items: int) -> str:
    """Summarise the batch and self-report model usage into the journal's usage slot."""
    record("summarize")
    satay.task_context().record_model_usage(
        model="demo-summarizer-v1", input_tokens=items * 40, output_tokens=120
    )
    return f"summary of {items} items"


@satay.task()
async def render_email(summary: str, reviewer: str) -> str:
    record("render_email")
    return f"<html>{summary} — approved by {reviewer}</html>"


@satay.workflow
async def publish_digest(summary_and_reviewer: list[str]) -> str:
    """A linked child workflow, so the run tree has a second level to open."""
    summary, reviewer = summary_and_reviewer
    return await render_email(summary, reviewer)


@satay.workflow
async def morning_digest(topic: str) -> dict[str, object]:
    """Fan out, park overnight, wait for approval, then hand off to a child workflow."""
    feeds = await satay.map(fetch_feed, SOURCES, key=source_key)
    total = sum(int(feed["items"]) for feed in feeds)
    summary = await summarize(total)

    await satay.sleep(timedelta(hours=8))  # parks until the send window opens

    approval = await satay.wait_for_event(
        PublishApproval, key=APPROVAL_KEY, timeout=timedelta(hours=12)
    )
    if approval is None or not approval.approved:
        return {"topic": topic, "published": False, "reason": "no approval"}

    child = await satay.start_child(publish_digest, [summary, approval.reviewer])
    body: str = await child.result()
    return {"topic": topic, "published": True, "items": total, "bytes": len(body)}


@satay.task(retries=1)
async def fetch_paywalled_feed(source: str) -> dict[str, object]:
    """A feed that never answers, so this run ends ``failed`` with a traceback."""
    record(f"fetch:{source}")
    raise PermissionError(f"{source} returned 402 Payment Required")


@satay.workflow
async def paywalled_digest(topic: str) -> dict[str, object]:
    """A one-task run that exhausts its retries — the failed run in the run list."""
    return await fetch_paywalled_feed("premium-wire")


def resolve_workdir() -> tuple[Path, bool]:
    """Where these runs' journals live, and whether they outlive the process.

    An explicit argument or ``SATAY_DATA_DIR`` means the caller wants the journal kept
    (so Studio can open it); with neither, fall back to a throwaway temp directory so the
    file stays self-contained wherever it is downloaded and run.
    """
    override = sys.argv[1] if len(sys.argv) > 1 else os.environ.get(DATA_DIR_ENV_VAR)
    if override:
        workdir = Path(override).expanduser().resolve()
        workdir.mkdir(parents=True, exist_ok=True)
        return workdir, True
    return Path(tempfile.mkdtemp(prefix="satay-studio-")), False


async def settle(factory: Any, clock: ManualClock, *, step: float = 61.0) -> Any:
    """Await ``factory()``, advancing ``clock`` through any retry backoff it waits on.

    Backoff sleeps on the injected clock, so under a ``ManualClock`` someone has to move
    time forward — see ``tests/conftest.py``'s ``drain`` fixture, which does the same for
    tests. Used for both the workflow drive and the worker ticks, since a tick can re-drive
    a run straight into a backoff wait.
    """
    task = asyncio.ensure_future(factory())
    try:
        for _ in range(500):
            for _ in range(4):
                await asyncio.sleep(0)
            if task.done():
                return await task
            if clock.pending_sleepers:
                clock.advance(step)
    finally:
        if not task.done():
            task.cancel()
    raise RuntimeError("the run never settled — is something waiting on real time?")


def recorded_feed_keys(events: list[Event]) -> list[str]:
    """The fan-out key of every feed whose result is durably on the journal."""
    return [
        event.payload["key"]
        for event in events
        if event.type is EventType.TASK_COMPLETED and "key" in event.payload
    ]


async def build_the_interesting_run(store: SQLiteStore, clock: ManualClock) -> str:
    """Drive ``morning_digest`` through a crash, a timer, an event, and a child run."""
    worker = TimerEventWorker(store=store, clock=clock)

    # Kill the worker the moment the first fan-out item commits its result: the run is
    # mid-execution, so resuming it writes WorkflowResumed — the ⚡ on the timeline.
    injector = FaultInjector()
    injector.crash_after("TaskCompleted")
    handle = satay.start(morning_digest, TOPIC, store=store, clock=clock, injector=injector)
    print(f"1. fan out over {len(SOURCES)} feeds ({FLAKY_SOURCE} needs a retry), then crash")
    try:
        await settle(handle.result, clock)
    except SimulatedCrash as exc:
        print(f"   worker died: {exc}")
    recorded = recorded_feed_keys(list(await store.read_events(handle.run_id)))
    before = {name: n for name, n in EXECUTIONS.items() if name.startswith("fetch:")}
    print(f"   durably recorded before the crash: {recorded}")
    print(f"   feeds actually fetched: {before}")

    print("2. restart the same run — recorded feeds are reused, unresolved ones re-run")
    # Passing the same run_id is what makes this a *resume* rather than a second run.
    resumed = satay.start(morning_digest, TOPIC, run_id=handle.run_id, store=store, clock=clock)
    parked = await settle(resumed.result, clock)
    print(f"   drive returned {parked}; status {await resumed.status()} (parked on the 8h sleep)")
    for source in SOURCES:
        key = source_key(source)
        if key in recorded:
            note = "REUSED from the journal — never fetched twice"
        elif before.get(f"fetch:{source}"):
            note = "was in flight, uncommitted, when it died — fetched again"
        else:
            note = "had not started yet — fetched on the restart"
        print(f"     {key:<20} fetched {EXECUTIONS[f'fetch:{source}']}x — {note}")
    print("   (at-least-once: only a *committed* result is reused, which is exactly why a")
    print("    retryable side-effecting task has to declare idempotent=True)")

    print("3. eight hours later: advance the clock and let the worker fire the timer")
    clock.advance(8 * 3600)
    print(f"   tick woke {await settle(lambda: worker.tick(), clock)} run(s)")
    print(f"   status {await resumed.status()} (now parked on the approval event)")

    print("4. approve it: send_event, then one more tick delivers it")
    await satay.send_event(
        PublishApproval(approved=True, reviewer="dana"), key=APPROVAL_KEY, store=store
    )
    print(f"   tick woke {await settle(lambda: worker.tick(), clock)} run(s)")
    result = await resumed.result()
    print(f"   result: {result}")
    print(f"   status: {await resumed.status()}")
    return resumed.run_id


async def build_the_failed_run(store: SQLiteStore, clock: ManualClock) -> str:
    """Drive a run into terminal failure, for the side-by-side in the run list."""
    handle = satay.start(paywalled_digest, "premium", store=store, clock=clock)
    print("5. and one run that fails outright, to compare against")
    try:
        await settle(handle.result, clock)
    except WorkflowFailedError as exc:
        print(f"   failed with {exc.error_type}: {exc.error_message}")
    return handle.run_id


def print_walkthrough(workdir: Path, durable: bool, run_id: str, failed_run_id: str) -> None:
    """Print the copy-pasteable Studio walkthrough for the run just produced."""
    data_dir = f"--data-dir {workdir}"
    print("\n" + "=" * 78)
    print("open it in Satay Studio")
    print("=" * 78)
    if not durable:
        print(
            f"\nHEADS UP: this ran without SATAY_DATA_DIR, so the journal is in a temp dir\n"
            f"({workdir}) that your OS may clear at any time. For a real look, re-run as:\n"
            f"    SATAY_DATA_DIR=.satay-demo uv run python examples/studio_walkthrough.py"
        )
    print(f"""
1. Boot the dev stack (worker + SQLite + read API + Studio, one process):

       uv run --extra studio satay dev {data_dir}

2. It prints a tokenized URL:

       Satay Studio:  http://127.0.0.1:{STUDIO_DEFAULT_PORT}/?token=<session-token>

   Open it *with* the ?token= query string. The SPA reads the token out of its own
   location and sends it on every request; the same URL without the query string gets
   a 401. The token is minted per `satay dev` session, so it changes on every boot.

   (If you pipe `satay dev` into a log file, run it with PYTHONUNBUFFERED=1 — its stdout
   is block-buffered when it is not a terminal, and the URL will sit in the buffer.)

3. In the run list, open  {run_id}  (morning_digest, completed) and look for:

     - the ⚡ marker on WorkflowResumed — where the worker died and the run was resumed
     - {len(SOURCES)} fan-out items under fetch_feed, each identified by key=source-<feed>,
       and the retried attempt on key={source_key(FLAKY_SOURCE)} (attempt 1 failed, 2 completed)
     - TimerCreated / WorkflowWaiting / TimerFired — the 8-hour sleep, parked and woken
     - EventWaitStarted / ExternalEventReceived — the approval that unblocked it
     - the child run under ChildWorkflowScheduled (publish_digest) in the run tree
     - the model usage summarize self-reported, in the task detail

4. Then open  {failed_run_id}  (paywalled_digest, failed): two attempts and a recorded
   traceback. "Fork this run" branches it at a chosen event — the prefix is copied, the
   rest re-runs under whatever your code says now — and "Compare runs" diffs the fork
   against its source by durable-call identity.

5. Same journal without a browser:

       uv run satay runs show {run_id} {data_dir}

   (`satay runs show` renders the V1 event subset in full detail and later event types as
   bare type lines, by design — Studio is where the rest is rendered.)

6. Scripting the read API instead? Every request needs the session-token header — it is
   `{TOKEN_HEADER_DISPLAY}`, not `Authorization: Bearer`:

       curl -H '{TOKEN_HEADER_DISPLAY}: <session-token>' \\
            http://127.0.0.1:{STUDIO_DEFAULT_PORT}/runs/{run_id}/timeline

   Also available: /runs, /runs/<id>/tree, /runs/<id>/tasks/<identity>,
   /runs/<id>/compare?other_run_id=<id>.
""")


async def main() -> None:
    workdir, durable = resolve_workdir()
    store = SQLiteStore.open(db_path(workdir))
    clock = ManualClock()

    print("Satay — building a run worth looking at")
    print(f"data dir: {workdir}\n")

    run_id = await build_the_interesting_run(store, clock)
    failed_run_id = await build_the_failed_run(store, clock)

    events = list(await store.read_events(run_id))
    print(f"\nthe run: {len(events)} events, {len({e.type for e in events})} distinct types")
    print(f"  interruption (⚡) at seq: {sorted(interruption_seqs(events))}")
    print(f"  recorded model usage: {model_usage(events)}")
    child = next((e for e in events if e.type is EventType.CHILD_WORKFLOW_SCHEDULED), None)
    if child is not None:
        print(f"  child run: {child.payload['child_run_id']} ({child.payload['workflow_name']})")
    print(f"  runs in the data dir: {len(await store.list_runs())}\n")
    print(render_timeline(events, run_id=run_id))
    store.close()

    print_walkthrough(workdir, durable, run_id, failed_run_id)


if __name__ == "__main__":
    asyncio.run(main())
