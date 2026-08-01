"""E2E: ``examples/agentic_dag_demo.py`` still demonstrates what its docstring promises.

The generic sweep in ``test_examples.py`` proves every example exits 0 and leaves a
coherent journal. This module asserts the *agentic* claims specifically, and only through
observable outcomes (ADR-0011): recorded model usage, per-key fan-out completions, the
approval event and its timeout twin, the ``RunForked`` lineage, and the printed ledger.
Replay internals are nobody's business here.

The whole thing runs with **no network and no API key** — which is the example's headline
claim, not a testing convenience. The model call lives behind a protocol whose default
implementation is a deterministic fake, so a run is reproducible, replayable and cheap. If
that ever stops being true, this module is where it shows up.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from satay.config import DATA_DIR_ENV_VAR, db_path
from satay.journal.events import Event, EventType, RunStatus
from satay.journal.store import SQLiteStore
from satay.journal.timeline import interruption_seqs, model_usage

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = REPO_ROOT / "examples" / "agentic_dag_demo.py"

#: The nested data dir the example puts the fork pair in (see its ``fork_workdir``).
FORK_SUBDIR = "reprompt"

#: The fan-out key of the research question whose answers are unparseable twice over.
FLAKY_KEY = "q-security"


def run_example(data_dir: Path) -> str:
    """Run the example as a subprocess with **no** API key in the environment.

    Scrubbing the provider variables is the point: the example must never reach for a
    network, and a machine that happens to have credentials exported must not change the
    result.
    """
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


def payloads(events: list[Event], event_type: EventType) -> list[dict[str, object]]:
    return [event.payload for event in events if event.type is event_type]


def attempts(events: list[Event], key: str) -> list[int]:
    """Attempt numbers recorded against one fan-out key, in journal order."""
    return [
        int(payload["attempt"])
        for payload in payloads(events, EventType.TASK_ATTEMPT_STARTED)
        if payload.get("key") == key
    ]


def completed_keys(events: list[Event]) -> list[str]:
    return [
        str(payload["key"])
        for payload in payloads(events, EventType.TASK_COMPLETED)
        if "key" in payload
    ]


# -- the approved run: fan-out, retries, a crash, the gate, then synthesis ---------


async def test_approved_dossier_fans_out_retries_and_clears_the_gate(tmp_path: Path) -> None:
    """The headline shape: five keyed research calls, a retried one, an approval, a write-up."""
    stdout = run_example(tmp_path)
    runs = await read_runs(tmp_path)

    status, events = runs["vendor_dossier"]
    assert status == RunStatus.COMPLETED.value

    # Fan-out: one keyed durable call per sub-question, each completing exactly once.
    keys = completed_keys(events)
    assert len(set(keys)) == 5, f"expected five keyed research answers, got {sorted(set(keys))}"
    assert len(keys) == len(set(keys)), "a research question completed twice — reuse is broken"

    # Retries + backoff on the flaky model call: three attempts, the first two recorded as
    # failures with a capped backoff delay, and the whole thing straddling a crash.
    assert attempts(events, FLAKY_KEY) == [1, 2, 3]
    failures = [p for p in payloads(events, EventType.TASK_ATTEMPT_FAILED) if p.get("key")]
    assert len(failures) == 2
    for payload in failures:
        assert payload["key"] == FLAKY_KEY
        error = payload["error"]
        assert isinstance(error, dict)
        assert error["type"] == "MalformedResponseError"
        delay = payload["next_delay"]
        assert isinstance(delay, float)
        assert 0.0 <= delay <= 60.0  # capped exponential backoff (ADR-0006)
    assert interruption_seqs(events), "the crash-and-resume ⚡ marker is missing"

    # The approval gate sits between the fan-out and the expensive call: the event arrives,
    # and only then is `synthesize` scheduled at all.
    assert payloads(events, EventType.EVENT_WAIT_STARTED)
    assert payloads(events, EventType.EXTERNAL_EVENT_RECEIVED)
    approval_seq = max(e.seq for e in events if e.type is EventType.EXTERNAL_EVENT_RECEIVED)
    synthesis_seqs = [
        e.seq
        for e in events
        if e.type is EventType.TASK_SCHEDULED and e.payload.get("task_name") == "synthesize"
    ]
    assert synthesis_seqs, "the approved run never reached synthesis"
    assert min(synthesis_seqs) > approval_seq, "synthesis was scheduled before the approval"

    assert "published by dana" in stdout


async def test_usage_is_recorded_per_attempt_for_every_model_call(tmp_path: Path) -> None:
    """``ctx.record_model_usage`` is what makes Studio able to price a run."""
    run_example(tmp_path)
    runs = await read_runs(tmp_path)
    _, events = runs["vendor_dossier"]

    entries = model_usage(events)
    assert entries, "no model usage recorded — Studio would show this run as free"
    assert {entry["model"] for entry in entries} == {"fake-scribe-1"}
    for entry in entries:
        assert int(entry["input_tokens"]) > 0
        assert int(entry["output_tokens"]) > 0
        assert entry["attempt"] >= 1  # the example's own extra field, carried verbatim
        assert entry["usd"] >= 0.0

    # The retried question reports one entry per attempt it actually made, failed ones
    # included: three attempts, three billable calls, three usage entries. Two of them ride
    # on TaskAttemptFailed, which is the only reason the failed ones are here at all — the
    # task records at the moment of the charge, not after the parse (KAN-479).
    flaky = [
        (event.type, entry)
        for event in events
        if event.payload.get("key") == FLAKY_KEY
        for entry in event.payload.get("usage", [])  # type: ignore[union-attr]
    ]
    assert [entry["attempt"] for _, entry in flaky] == [1, 2, 3]
    assert [event_type for event_type, _ in flaky] == [
        EventType.TASK_ATTEMPT_FAILED,
        EventType.TASK_ATTEMPT_FAILED,
        EventType.TASK_COMPLETED,
    ]


# -- the timeout branch of the gate ------------------------------------------------


async def test_unapproved_dossier_times_out_and_never_pays_for_synthesis(tmp_path: Path) -> None:
    """Nobody approves: the wait resolves to ``None`` and the run escalates instead."""
    stdout = run_example(tmp_path)
    runs = await read_runs(tmp_path)

    status, events = runs["unattended_dossier"]
    assert status == RunStatus.COMPLETED.value  # a timeout is a branch, not a failure

    assert payloads(events, EventType.EVENT_WAIT_STARTED)
    assert payloads(events, EventType.TIMER_FIRED), "the review window never expired"
    assert not payloads(events, EventType.EXTERNAL_EVENT_RECEIVED)

    # The whole point of putting the gate before the write-up: no approval, no spend.
    scheduled = {p.get("task_name") for p in payloads(events, EventType.TASK_SCHEDULED)}
    assert "synthesize" not in scheduled
    assert "escalated: no reviewer within 4h" in stdout


# -- fail-fast fan-out -------------------------------------------------------------


async def test_dead_source_fails_the_whole_fan_out_and_its_cost_is_still_recorded(
    tmp_path: Path,
) -> None:
    """ADR-0020 fail-fast, and the money it takes down with it.

    The dead question exhausts its retries; the ``map`` raises; the run fails. Its
    siblings' answers survive on the journal — and so does the dead question's bill, since
    usage is flushed onto ``TaskAttemptFailed`` too (KAN-479). This is the run that used to
    under-report its own spend by 77%, so assert the tokens are there rather than trusting
    the prose the example prints.
    """
    stdout = run_example(tmp_path)
    runs = await read_runs(tmp_path)

    status, events = runs["brittle_dossier"]
    assert status == RunStatus.FAILED.value

    failed = payloads(events, EventType.WORKFLOW_FAILED)
    assert len(failed) == 1
    error = failed[0]["error"]
    assert isinstance(error, dict)
    assert error["type"] == "MalformedResponseError"

    # retries=2 → three attempts, all three failed, none of them completed.
    assert attempts(events, "q-litigation") == [1, 2, 3]
    dead_failures = [
        p for p in payloads(events, EventType.TASK_ATTEMPT_FAILED) if p.get("key") == "q-litigation"
    ]
    assert len(dead_failures) == 3
    assert "q-litigation" not in completed_keys(events)

    # Siblings that got there first are durably recorded — fail-fast loses the run, not
    # the results — but the workflow has no way to proceed on a partial fan-out.
    assert set(completed_keys(events)) == {"q-pricing", "q-references"}

    # The dead question attaches ~21k context tokens per attempt, and every one of those
    # three attempts is priced on the journal even though the task never completed.
    dead = [
        entry
        for payload in payloads(events, EventType.TASK_ATTEMPT_FAILED)
        if payload.get("key") == "q-litigation"
        for entry in payload.get("usage", [])  # type: ignore[union-attr]
    ]
    assert [entry["attempt"] for entry in dead] == [1, 2, 3]
    assert min(int(entry["input_tokens"]) for entry in dead) > 20_000

    # And the run's aggregate — "what did this cost" — is the whole bill, not just the two
    # answers that survived. `include_failed_attempts=False` is what asks the old question.
    complete = sum(int(e["input_tokens"]) for e in model_usage(events))
    succeeded = sum(
        int(e["input_tokens"]) for e in model_usage(events, include_failed_attempts=False)
    )
    assert complete == succeeded + sum(int(e["input_tokens"]) for e in dead)
    assert succeeded < complete / 3  # the failed source was the bulk of the spend
    assert "all of it is on the journal" in stdout


# -- fork the finished dossier under a changed prompt ------------------------------


async def test_fork_reruns_only_the_synthesis_under_the_changed_prompt(tmp_path: Path) -> None:
    """V7: a fork reuses the copied research prefix and re-runs just the write-up.

    A prompt is data, not schedule, so changing it leaves the durable-call sequence
    identical and the fork replays cleanly under strict nondeterminism detection.
    """
    stdout = run_example(tmp_path)
    forkdir = tmp_path / FORK_SUBDIR
    assert db_path(forkdir).exists(), "the fork pair's data dir is missing"

    store = SQLiteStore.open(db_path(forkdir))
    try:
        run_ids = await store.list_runs()
        assert len(run_ids) == 2, f"expected a source run and its fork, got {run_ids}"
        journals = {run_id: list(await store.read_events(run_id)) for run_id in run_ids}
        statuses = {}
        for run_id in run_ids:
            record = await store.get_run(run_id)
            assert record is not None
            assert record.workflow_name == "vendor_dossier"
            statuses[run_id] = record.status.value
    finally:
        store.close()

    assert set(statuses.values()) == {RunStatus.COMPLETED.value}

    forked = [run_id for run_id, events in journals.items() if _run_forked(events)]
    assert len(forked) == 1, "exactly one of the two runs should carry RunForked lineage"
    fork_id = forked[0]
    source_id = next(run_id for run_id in journals if run_id != fork_id)

    lineage = _run_forked(journals[fork_id])
    assert lineage is not None
    assert lineage.payload["source_run_id"] == source_id

    # The fork point sits before synthesis, so the copied research is a journal hit and only
    # the write-up re-runs: after the RunForked marker, one attempt, and it is synthesize.
    after_fork = [
        e
        for e in journals[fork_id]
        if e.type is EventType.TASK_ATTEMPT_STARTED and e.seq > lineage.seq
    ]
    assert [e.payload["task_name"] for e in after_fork] == ["synthesize"]

    # A fresh fork is a new run, not a crash recovery: no ⚡ (ADR-0004).
    assert interruption_seqs(journals[fork_id]) == set()

    # And the source is untouched: it still says what it said, under the old prompt.
    assert "Recommendation: proceed." in stdout
    assert "Recommendation: hold pending a second source." in stdout


def _run_forked(events: list[Event]) -> Event | None:
    """This run's own ``RunForked`` event, or ``None`` if it is not a fork."""
    forked = [e for e in events if e.type is EventType.RUN_FORKED]
    return max(forked, key=lambda e: e.seq) if forked else None


# -- the seam itself ---------------------------------------------------------------


def test_the_example_needs_no_provider_and_adds_no_dependency() -> None:
    """The constraint that makes the example CI-runnable at all.

    The model adapter is out of scope for the package (ADR-0016), so the protocol, the
    fake and the optional real client all live in the example file, and nothing at module
    scope may import a provider SDK. ``tests/integration/test_import_hygiene.py`` guards
    the core; this guards the example.
    """
    source = EXAMPLE.read_text(encoding="utf-8")
    module_level_imports = [
        line
        for line in source.splitlines()
        if line.startswith(("import ", "from ")) and "satay" not in line
    ]
    for line in module_level_imports:
        assert "anthropic" not in line and "openai" not in line, (
            f"provider SDK imported at module scope: {line!r}"
        )
    # The real client exists, but only behind a function-local import.
    assert "from anthropic import AsyncAnthropic" in source
    assert "        from anthropic import" in source, "the provider import must stay function-local"
