"""E2E: ``examples/best_of_n_demo.py`` still demonstrates collect-mode fan-out (ADR-0027).

``tests/e2e/test_collect_fanout.py`` tests the *primitive*: the returned list, the error
type, the terminal ``TaskFailed``, the replay hit. This module tests the **example** — that
the file a reader downloads still shows those things happening to a run they would
recognise, and still prints the ledger the cookbook page quotes.

The claims, in the order the example makes them:

1. the fail-fast default kills the bake-off and strands three finished drafts,
2. ``return_exceptions=True`` over the same five candidates completes, judges the
   survivors, and records each dead candidate as a terminal ``TaskFailed``,
3. a recorded ``TaskFailed`` replays as a hit, so a crash does not buy the same failure
   twice.

Observable outcomes only (ADR-0011): run statuses, journal events, recorded usage, printed
output. Never replay internals. And no network and no API key, which is the example's own
claim rather than a testing convenience.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from satay.config import DATA_DIR_ENV_VAR, db_path
from satay.journal.events import Event, EventType, RunStatus
from satay.journal.store import SQLiteStore
from satay.journal.timeline import interruption_seqs, model_usage

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = REPO_ROOT / "examples" / "best_of_n_demo.py"

#: The two candidates that never produce a draft, and the three that do.
DEAD = {"c-refund", "c-legal"}
SURVIVORS = {"c-policy", "c-goodwill", "c-escalate"}


def run_example(data_dir: Path) -> str:
    """Run the example as a subprocess with **no** provider credentials in the environment."""
    env = {**os.environ, "PYTHONUNBUFFERED": "1", DATA_DIR_ENV_VAR: str(data_dir)}
    for leaked in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "SATAY_DEMO_MODEL"):
        env.pop(leaked, None)
    proc = subprocess.run(
        [sys.executable, str(EXAMPLE)],
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO_ROOT,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, (
        f"exited {proc.returncode}\n--- stdout ---\n{proc.stdout[-4000:]}\n"
        f"--- stderr ---\n{proc.stderr[-4000:]}"
    )
    return proc.stdout


async def read_runs(data_dir: Path) -> dict[str, tuple[str, list[Event]]]:
    """Every run in ``data_dir`` as ``{workflow_name: (status, events)}``."""
    store = SQLiteStore.open(db_path(data_dir))
    try:
        runs: dict[str, tuple[str, list[Event]]] = {}
        for run_id in await store.list_runs():
            record = await store.get_run(run_id)
            assert record is not None
            events = list(await store.read_events(run_id))
            runs[record.workflow_name] = (record.status.value, events)
        return runs
    finally:
        store.close()


def keys_of(events: list[Event], event_type: EventType) -> list[str]:
    return [
        str(event.payload["key"])
        for event in events
        if event.type is event_type and "key" in event.payload
    ]


def usd_of(events: list[Event], keys: set[str]) -> float:
    """What the journal says was spent on a set of fan-out keys, failed attempts included."""
    return sum(
        float(entry.get("usd", 0.0))
        for event in events
        if event.payload.get("key") in keys
        for entry in event.payload.get("usage", [])  # type: ignore[union-attr]
    )


# -- part 1: the fail-fast default -------------------------------------------------


async def test_fail_fast_run_dies_and_strands_the_drafts_that_finished(tmp_path: Path) -> None:
    """The default: one dead candidate ends the run, and the judge never sees the others."""
    stdout = run_example(tmp_path)
    status, events = (await read_runs(tmp_path))["strict_bake_off"]

    assert status == RunStatus.FAILED.value
    failed = [e.payload for e in events if e.type is EventType.WORKFLOW_FAILED]
    assert len(failed) == 1
    error = failed[0]["error"]
    assert isinstance(error, dict)
    assert error["type"] in {"MalformedResponseError", "RefusedError"}

    # Three drafts committed and are on the journal, which is the cost of the default.
    assert set(keys_of(events, EventType.TASK_COMPLETED)) == SURVIVORS
    assert usd_of(events, SURVIVORS) > 0.0

    # Fail-fast journals are byte-identical to what they were before ADR-0027: the run's
    # own WorkflowFailed is the terminal record, and no TaskFailed is written.
    assert [e for e in events if e.type is EventType.TASK_FAILED] == []

    # And nothing downstream of the fan-out ran, so the three drafts bought nothing.
    scheduled = {e.payload.get("task_name") for e in events if e.type is EventType.TASK_SCHEDULED}
    assert "judge" not in scheduled
    assert "nothing shipped" in stdout


# -- part 2: one argument ----------------------------------------------------------


async def test_collect_mode_ships_a_reply_from_the_surviving_drafts(tmp_path: Path) -> None:
    """``return_exceptions=True`` over the same five candidates: three survive, one ships."""
    stdout = run_example(tmp_path)
    runs = await read_runs(tmp_path)
    status, events = runs["reply_bake_off"]

    assert status == RunStatus.COMPLETED.value
    assert set(keys_of(events, EventType.TASK_COMPLETED)) == SURVIVORS

    # The judge ran, and it ran after the whole fan-out settled.
    judge_scheduled = [
        e.seq
        for e in events
        if e.type is EventType.TASK_SCHEDULED and e.payload.get("task_name") == "judge"
    ]
    assert len(judge_scheduled) == 1
    last_draft = max(
        e.seq
        for e in events
        if e.type in {EventType.TASK_COMPLETED, EventType.TASK_FAILED} and "key" in e.payload
    )
    assert judge_scheduled[0] > last_draft
    assert [e for e in events if e.type is EventType.WORKFLOW_COMPLETED]

    # The same input that killed the fail-fast run completes here. That is the lesson.
    assert runs["strict_bake_off"][0] == RunStatus.FAILED.value
    assert "winner escalate" in stdout


async def test_each_collected_failure_is_a_terminal_task_failed_on_the_journal(
    tmp_path: Path,
) -> None:
    """The half of ADR-0027 people miss: a survivable failure is a *recorded* failure."""
    run_example(tmp_path)
    _, events = (await read_runs(tmp_path))["reply_bake_off"]

    failed = [e for e in events if e.type is EventType.TASK_FAILED]
    assert {str(e.payload["key"]) for e in failed} == DEAD
    assert len(failed) == len(DEAD), "one terminal record per logical call, no more"

    for event in failed:
        assert event.payload["task_name"] == "draft"
        error = event.payload["error"]
        assert isinstance(error, dict)
        assert error["message"]
        assert error["traceback"]
    # The class *name* travels, one per failure mode, which is what the example shows by
    # having two: the slot type is uniformly TaskFailedError, `error_type` is not.
    assert {str(e.payload["error"]["type"]) for e in failed} == {  # type: ignore[index]
        "MalformedResponseError",
        "RefusedError",
    }

    # Every attempt is recorded too — retries are unchanged by collect mode.
    assert sorted(keys_of(events, EventType.TASK_ATTEMPT_FAILED)) == sorted(list(DEAD) * 2)


async def test_the_dead_candidates_are_priced_and_the_survivors_are_reachable(
    tmp_path: Path,
) -> None:
    """The money argument the example prints: both runs paid, only one got anything back."""
    stdout = run_example(tmp_path)
    runs = await read_runs(tmp_path)
    _, collected = runs["reply_bake_off"]
    _, strict = runs["strict_bake_off"]

    # Usage rides on TaskAttemptFailed as well as TaskCompleted, so a candidate that never
    # produced a draft still prices itself (KAN-479).
    assert usd_of(collected, DEAD) > 0.0
    assert usd_of(collected, SURVIVORS) > 0.0
    # The two big retrieval corpora sit on the dead candidates, twice each.
    assert usd_of(collected, DEAD) > usd_of(collected, SURVIVORS)

    # The fail-fast run paid for the same dead candidates and got nothing for the three
    # drafts it did finish. Those three cost both runs exactly the same.
    assert usd_of(strict, DEAD) > 0.0
    assert usd_of(collected, DEAD) >= usd_of(strict, DEAD)
    assert usd_of(strict, SURVIVORS) == pytest.approx(usd_of(collected, SURVIVORS))

    entries = model_usage(collected)
    assert entries, "no recorded usage — the ledger the page quotes would be empty"
    assert {entry["model"] for entry in entries} == {"fake-drafter-1"}
    assert f"${usd_of(strict, SURVIVORS):.4f} of finished drafts, unreachable" in stdout


# -- part 3: a recorded failure replays as a hit -----------------------------------


async def test_a_recorded_failure_is_not_paid_for_twice_after_a_crash(tmp_path: Path) -> None:
    """Crash the instant a failure becomes terminal; the resume must not re-run it."""
    run_example(tmp_path)
    status, events = (await read_runs(tmp_path))["interrupted_bake_off"]

    assert status == RunStatus.COMPLETED.value
    assert interruption_seqs(events), "the crash-and-resume ⚡ marker is missing"
    resumed_at = min(e.seq for e in events if e.type is EventType.WORKFLOW_RESUMED)

    # One failure was terminal before the crash. Whichever it was, the resume must not have
    # started another attempt on it: a recorded TaskFailed is a replay hit.
    settled_early = {
        str(e.payload["key"])
        for e in events
        if e.type is EventType.TASK_FAILED and e.seq < resumed_at
    }
    assert settled_early, "the crash did not land after a TaskFailed — the demo lost its point"
    after_the_crash = {
        str(e.payload["key"])
        for e in events
        if e.type is EventType.TASK_ATTEMPT_STARTED and e.seq > resumed_at and "key" in e.payload
    }
    assert settled_early.isdisjoint(after_the_crash)

    # And each failure is still recorded exactly once across both drives.
    assert sorted(keys_of(events, EventType.TASK_FAILED)) == sorted(DEAD)
    # The drafts that had committed were reused rather than re-run, as they always were.
    assert sorted(keys_of(events, EventType.TASK_COMPLETED)) == sorted(SURVIVORS)


# -- the seam itself ---------------------------------------------------------------


def test_the_example_needs_no_provider_and_adds_no_dependency() -> None:
    """Satay ships no model adapters (ADR-0016), so the seam and its fake live in the file."""
    source = EXAMPLE.read_text(encoding="utf-8")
    for line in source.splitlines():
        if line.startswith(("import ", "from ")):
            assert "anthropic" not in line and "openai" not in line, (
                f"provider SDK imported at module scope: {line!r}"
            )
    assert "        from anthropic import" in source, "the provider import must stay function-local"
