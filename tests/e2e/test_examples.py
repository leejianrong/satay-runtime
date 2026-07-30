"""E2E: the ``examples/`` directory actually runs, and still shows what it claims.

Nothing rots faster than an example. Each file under ``examples/`` is executed here as a
real subprocess against a temp data dir, and the journal it leaves behind is asserted on:
statuses, event types, per-key fan-out completions, ⚡ resume markers, recorded usage —
observable outcomes only, never private replay internals (ADR-0011). If an API change
breaks an example, this module goes red instead of a user's first five minutes.

The examples own their determinism controls: each one injects a ``ManualClock`` (so an
8-hour ``sleep`` and a full retry-backoff schedule resolve instantly) and a
``FaultInjector`` for the crash phases, which is exactly why a whole set of durable
workflows can be exercised here in about a second of wall clock.

:data:`EXAMPLES` is **discovered, not listed**, so a new example is covered the moment it
lands: it has to exit 0 and leave a coherent journal behind, or this module fails.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from satay.config import DATA_DIR_ENV_VAR, db_path
from satay.journal.events import Event, EventType, RunStatus
from satay.journal.store import SQLiteStore
from satay.journal.timeline import interruption_seqs, model_usage

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLES_DIR = REPO_ROOT / "examples"

#: Every example, discovered rather than enumerated (the anti-rot property).
EXAMPLES = sorted(path.name for path in EXAMPLES_DIR.glob("*.py"))

#: Generous ceiling: an example that trips it is waiting on real time, which is a bug.
EXAMPLE_TIMEOUT_SECONDS = 120


def test_examples_directory_is_not_empty() -> None:
    """Guards the discovery above: a bad glob must not silently make this module vacuous."""
    assert len(EXAMPLES) >= 5


def run_example(
    name: str,
    *,
    data_dir: Path | None = None,
    argv: tuple[str, ...] = (),
    cwd: Path | None = None,
) -> str:
    """Run one example as a subprocess and return its stdout, asserting a clean exit.

    ``data_dir`` is passed the way a user would pass it — through ``SATAY_DATA_DIR`` —
    unless the caller wants the argument form instead. With neither, the example must
    fall back to its own throwaway temp directory.
    """
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    if data_dir is not None:
        env[DATA_DIR_ENV_VAR] = str(data_dir)
    else:
        env.pop(DATA_DIR_ENV_VAR, None)

    proc = subprocess.run(
        [sys.executable, str(EXAMPLES_DIR / name), *argv],
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd or REPO_ROOT,
        timeout=EXAMPLE_TIMEOUT_SECONDS,
        check=False,
    )
    assert proc.returncode == 0, (
        f"{name} exited {proc.returncode}\n--- stdout ---\n{proc.stdout[-4000:]}\n"
        f"--- stderr ---\n{proc.stderr[-4000:]}"
    )
    return proc.stdout


@dataclass(frozen=True)
class RunFacts:
    """The observable facts about one run: its status and its journal."""

    run_id: str
    status: str
    events: list[Event]

    @property
    def types(self) -> list[str]:
        return [event.type.value for event in self.events]

    def count(self, event_type: EventType) -> int:
        return sum(1 for event in self.events if event.type is event_type)

    def payloads(self, event_type: EventType) -> list[dict[str, Any]]:
        return [event.payload for event in self.events if event.type is event_type]

    def attempts(self, task_name: str) -> list[int]:
        """Attempt numbers recorded for ``task_name``, in order."""
        return [
            payload["attempt"]
            for payload in self.payloads(EventType.TASK_ATTEMPT_STARTED)
            if payload.get("task_name") == task_name
        ]

    def completed_keys(self) -> list[str]:
        """The fan-out key of every keyed item whose result is on the journal."""
        return [
            payload["key"]
            for payload in self.payloads(EventType.TASK_COMPLETED)
            if "key" in payload
        ]


async def read_journal(data_dir: Path) -> dict[str, RunFacts]:
    """Read every run in ``data_dir``, keyed by workflow name.

    Each example uses a distinct workflow per run, so the name is a stable handle for a
    test to assert on — unlike a generated ``run_id``.
    """
    store = SQLiteStore.open(db_path(data_dir))
    try:
        facts: dict[str, RunFacts] = {}
        for run_id in await store.list_runs():
            record = await store.get_run(run_id)
            assert record is not None
            assert record.workflow_name not in facts, "one run per workflow expected"
            facts[record.workflow_name] = RunFacts(
                run_id=run_id,
                status=record.status.value,
                events=list(await store.read_events(run_id)),
            )
        return facts
    finally:
        store.close()


# -- the discovered set ----------------------------------------------------------


@pytest.mark.parametrize("name", EXAMPLES)
async def test_example_runs_and_leaves_a_coherent_journal(name: str, tmp_path: Path) -> None:
    """Every example exits 0, writes its journal where it was told, and settles its runs."""
    data_dir = tmp_path / "data"
    stdout = run_example(name, data_dir=data_dir)

    assert db_path(data_dir).exists(), f"{name} wrote no journal to the given data dir"
    assert str(data_dir.resolve()) in stdout, f"{name} never says where its journal went"

    runs = await read_journal(data_dir)
    assert runs, f"{name} recorded no runs"
    # No run may be left mid-flight or parked: an example that ends with a run still
    # `running`/`waiting` is an example whose story did not finish.
    terminal = {RunStatus.COMPLETED.value, RunStatus.FAILED.value}
    assert {facts.status for facts in runs.values()} <= terminal, (
        f"{name} left non-terminal runs: { {name_: f.status for name_, f in runs.items()} }"
    )
    assert any(facts.status == RunStatus.COMPLETED.value for facts in runs.values())


@pytest.mark.parametrize("name", EXAMPLES)
def test_example_is_self_contained_without_a_data_dir(name: str, tmp_path: Path) -> None:
    """With no ``SATAY_DATA_DIR`` and no argument, an example must leave the cwd alone.

    That is the "curl it into any directory and run it" promise: the fallback is a
    throwaway temp dir, never a ``.satay`` scribbled into wherever you happened to be.
    """
    stdout = run_example(name, data_dir=None, cwd=tmp_path)

    assert "temp dir" in stdout
    assert not (tmp_path / ".satay").exists(), f"{name} polluted the working directory"


def test_example_accepts_the_data_dir_as_an_argument(tmp_path: Path) -> None:
    """The positional-path form works too (what ``make demo``-style wrappers can use)."""
    target = tmp_path / "explicit"
    stdout = run_example("fan_out_recovery_demo.py", data_dir=None, argv=(str(target),))

    assert db_path(target).exists()
    assert str(target.resolve()) in stdout


# -- retries + backoff -----------------------------------------------------------


async def test_retries_example_records_three_attempts_then_succeeds(tmp_path: Path) -> None:
    stdout = run_example("retries_backoff_demo.py", data_dir=tmp_path)
    runs = await read_journal(tmp_path)

    quote = runs["quote"]
    assert quote.status == RunStatus.COMPLETED.value
    assert quote.attempts("fetch_rate") == [1, 2, 3]  # fails twice, succeeds on the third
    failures = quote.payloads(EventType.TASK_ATTEMPT_FAILED)
    assert len(failures) == 2
    for payload in failures:
        assert payload["error"]["type"] == "RuntimeError"
        assert 0.0 <= payload["next_delay"] <= 60.0  # capped backoff (ADR-0006)
    assert quote.count(EventType.TASK_COMPLETED) == 2  # the retried fetch, then convert
    assert "attempt 3  SUCCEEDED" in stdout

    # Exhaustion is the other half of the story: the run fails with the LAST error.
    doomed = runs["doomed_quote"]
    assert doomed.status == RunStatus.FAILED.value
    assert doomed.attempts("fetch_from_dead_host") == [1, 2]  # retries=1 → two attempts
    assert doomed.types[-1] == EventType.WORKFLOW_FAILED.value
    assert doomed.payloads(EventType.WORKFLOW_FAILED)[0]["error"]["type"] == "ConnectionError"


# -- timers + events -------------------------------------------------------------


async def test_timers_example_covers_sleep_delivery_and_timeout(tmp_path: Path) -> None:
    stdout = run_example("timers_events_demo.py", data_dir=tmp_path)
    runs = await read_journal(tmp_path)
    assert all(facts.status == RunStatus.COMPLETED.value for facts in runs.values())

    # 1: a durable sleep parks on a timer and is woken by the worker.
    sleeping = runs["overnight_restock"]
    assert EventType.TIMER_CREATED.value in sleeping.types
    assert EventType.WORKFLOW_WAITING.value in sleeping.types
    assert EventType.TIMER_FIRED.value in sleeping.types
    # A graceful wake from a park is not an interruption — no ⚡ (ADR-0009/Q52).
    assert interruption_seqs(sleeping.events) == set()

    # 2: an external event unblocks the wait.
    delivered = runs["await_shipment"]
    assert EventType.EVENT_WAIT_STARTED.value in delivered.types
    assert EventType.EXTERNAL_EVENT_RECEIVED.value in delivered.types
    assert EventType.TIMER_FIRED.value not in delivered.types

    # 3: nobody sends anything, so the timeout resolves the wait instead.
    timed_out = runs["await_shipment_or_escalate"]
    assert EventType.TIMER_FIRED.value in timed_out.types
    assert EventType.EXTERNAL_EVENT_RECEIVED.value not in timed_out.types
    assert "escalated:" in stdout


# -- fan-out with crash recovery (the signature demo) ----------------------------


async def test_fan_out_example_reuses_completed_items_across_two_crashes(
    tmp_path: Path,
) -> None:
    """The headline guarantee: five items, two crashes, every item indexed exactly once."""
    stdout = run_example("fan_out_recovery_demo.py", data_dir=tmp_path)
    runs = await read_journal(tmp_path)

    batch = runs["index_batch"]
    assert batch.status == RunStatus.COMPLETED.value

    keys = batch.completed_keys()
    assert len(keys) == 5
    assert len(set(keys)) == 5, "an item completed twice — reuse is broken"
    assert batch.count(EventType.WORKFLOW_RESUMED) == 2  # two ⚡ markers, two restarts

    # The ledger the demo prints has to actually say what was reused.
    assert stdout.count("REUSED from the journal") == 2
    assert "5 executions in total" in stdout
    assert "Every document was indexed exactly once" in stdout


# -- the Studio walkthrough ------------------------------------------------------


async def test_studio_walkthrough_builds_a_rich_run_and_explains_how_to_open_it(
    tmp_path: Path,
) -> None:
    stdout = run_example("studio_walkthrough.py", data_dir=tmp_path)
    runs = await read_journal(tmp_path)

    digest = runs["morning_digest"]
    assert digest.status == RunStatus.COMPLETED.value
    # Interesting enough to be worth opening: a crash-and-resume, a keyed fan-out, a
    # timer, an event, a child run, and recorded model usage.
    assert interruption_seqs(digest.events), "no ⚡ — the walkthrough promises one"
    assert len(set(digest.completed_keys())) == 4
    assert EventType.TIMER_FIRED.value in digest.types
    assert EventType.EXTERNAL_EVENT_RECEIVED.value in digest.types
    assert model_usage(digest.events)[0]["model"] == "demo-summarizer-v1"

    # The child run is linked both ways, which is what the run tree renders.
    child = runs["publish_digest"]
    scheduled = digest.payloads(EventType.CHILD_WORKFLOW_SCHEDULED)[0]
    assert scheduled["child_run_id"] == child.run_id
    assert child.payloads(EventType.WORKFLOW_CREATED)[0]["parent_run_id"] == digest.run_id

    # A failed run too, so the run list has both outcomes to compare.
    assert runs["paywalled_digest"].status == RunStatus.FAILED.value

    # The walkthrough it prints has to be usable: the right command, the right data dir,
    # the tokenized URL, and the header the API actually authenticates with.
    assert f"satay dev --data-dir {tmp_path.resolve()}" in stdout
    assert "?token=" in stdout
    assert "X-Satay-Token" in stdout
    assert "Authorization: Bearer" in stdout  # named only to say it is NOT that
    assert digest.run_id in stdout
    assert runs["paywalled_digest"].run_id in stdout


# -- the V1 crash-recovery headline ----------------------------------------------


async def test_crash_recovery_example_reuses_the_recorded_step(tmp_path: Path) -> None:
    stdout = run_example("crash_recovery_demo.py", data_dir=tmp_path)
    runs = await read_journal(tmp_path)

    demo = runs["demo"]
    assert demo.status == RunStatus.COMPLETED.value
    assert demo.count(EventType.WORKFLOW_RESUMED) == 1  # one crash, one ⚡
    assert demo.count(EventType.TASK_COMPLETED) == 2  # each step recorded exactly once
    assert "REUSED, still 1" in stdout
