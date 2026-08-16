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
lands: it has to exit 0, leave a coherent journal behind, and stand on its own as a single
downloadable file, or this module fails.
"""

from __future__ import annotations

import ast
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

    Each example uses a distinct workflow per *started* run, so the name is a stable
    handle for a test to assert on — unlike a generated ``run_id``.

    **Forks are the exception, and they used to be forbidden by this function** (KAN-480).
    A fork is by construction a second run of the *same* workflow (ADR-0004: it copies a
    prefix of its source's journal), so an example that forks — which is the demo ADR-0025
    calls the wedge — collided with the uniqueness assertion and had to hide its fork in a
    separate data directory. That is backwards: the fork and its source belong side by
    side, which is exactly how Studio's compare view wants them.

    So a run carrying a ``RunForked`` marker is keyed ``<workflow>@fork``, then
    ``@fork2``, ``@fork3`` in whatever order the store lists them. **The order is the
    store's, not a promise**: runs are listed by ``created_at`` and an example driving a
    ``ManualClock`` stamps several runs at the same virtual instant, so a test with more
    than one fork must assert over the *set* of them (see
    :func:`test_fork_and_compare_example_reuses_the_prefix`) rather than on which one is
    ``@fork2``.

    Two *unforked* runs of one workflow are still an error. That is the case the original
    assertion was protecting against, and it is still worth protecting against: the key
    would be ambiguous and a test asserting on it would silently pick one of them.
    """
    store = SQLiteStore.open(db_path(data_dir))
    try:
        facts: dict[str, RunFacts] = {}
        for run_id in await store.list_runs():
            record = await store.get_run(run_id)
            assert record is not None
            events = list(await store.read_events(run_id))
            name = record.workflow_name
            if any(event.type is EventType.RUN_FORKED for event in events):
                nth = 1 + sum(1 for key in facts if key.startswith(f"{name}@fork"))
                name = f"{name}@fork" if nth == 1 else f"{name}@fork{nth}"
            assert name not in facts, (
                f"two runs of {record.workflow_name!r} that are not forks of anything — "
                "one run per workflow expected, so a test can name it"
            )
            facts[name] = RunFacts(run_id=run_id, status=record.status.value, events=events)
        return facts
    finally:
        store.close()


def forks_of(runs: dict[str, RunFacts], workflow: str) -> list[RunFacts]:
    """Every forked run of ``workflow`` in a journal, in unspecified order."""
    return [facts for name, facts in runs.items() if name.startswith(f"{workflow}@fork")]


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

    Half of the "curl it into any directory and run it" promise: the fallback is a
    throwaway temp dir, never a ``.satay`` scribbled into wherever you happened to be.
    The other half — that the file needs no sibling file — is
    :func:`test_example_imports_no_sibling_example_module`, because this one cannot see it.
    """
    stdout = run_example(name, data_dir=None, cwd=tmp_path)

    assert "temp dir" in stdout
    assert not (tmp_path / ".satay").exists(), f"{name} polluted the working directory"


@pytest.mark.parametrize("name", EXAMPLES)
def test_example_imports_no_sibling_example_module(name: str) -> None:
    """Each example is ONE file: it may import ``satay`` and the stdlib, nothing local.

    ``docs/RELEASING.md`` §4 and every cookbook page download a **single file** and run it
    (`curl -fsSL -O .../examples/<file>.py`), so a shared ``examples/_helper.py`` would
    break a user-facing, release-procedure-load-bearing promise.

    This has to be a static check, and that is the whole reason it exists. Running the
    examples cannot catch it: CPython puts the *script's own directory* on ``sys.path``, so
    ``import _helper`` resolves here no matter what cwd the subprocess is given, and the
    test above would stay green while a downloaded copy died on ``ModuleNotFoundError``.

    If a future slice decides the duplication between examples is worth more than the
    guarantee, that is a real decision someone may make — but it has to be made *here*,
    by deleting this test on purpose, rather than by quietly adding an import (KAN-482).
    """
    source = (EXAMPLES_DIR / name).read_text()
    siblings = {path.stem for path in EXAMPLES_DIR.glob("*.py")} - {Path(name).stem}

    for node in ast.walk(ast.parse(source, filename=name)):
        if isinstance(node, ast.ImportFrom):
            assert node.level == 0, f"{name} uses a relative import; it must be standalone"
            root = (node.module or "").split(".")[0]
        elif isinstance(node, ast.Import):
            root = node.names[0].name.split(".")[0]
        else:
            continue
        assert root not in siblings, (
            f"{name} imports the sibling example module {root!r}. Examples are downloaded "
            "one file at a time (docs/RELEASING.md §4), so this breaks curl-and-run."
        )


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


# -- the debugger wedge: fork a prefix, replay, compare ---------------------------


async def test_fork_and_compare_example_reuses_the_prefix(tmp_path: Path) -> None:
    """The wedge demo (KAN-656, ADR-0025): one bad run, three forks, one call re-run.

    Everything asserted here is an observable outcome (ADR-0011): run statuses, the
    ``RunForked`` lineage, which durable calls have a ``TaskAttemptStarted`` *above* the
    fork marker (the only ones whose body actually ran), and the recorded outputs the
    compare view aligns. No replay internals.
    """
    stdout = run_example("fork_and_compare_demo.py", data_dir=tmp_path)
    runs = await read_journal(tmp_path)

    source = runs["answer_ticket"]
    forks = forks_of(runs, "answer_ticket")
    assert source.status == RunStatus.COMPLETED.value
    assert len(forks) == 3, "the demo forks three times: the fix, the trap, and the redo"
    assert all(fork.status == RunStatus.COMPLETED.value for fork in forks)

    # The bad run is bad on purpose, and it *completed*. That is the whole premise: no
    # exception, no ⚡, just a wrong answer that a stack trace cannot find.
    assert source.status == RunStatus.COMPLETED.value
    assert EventType.WORKFLOW_FAILED.value not in source.types
    assert interruption_seqs(source.events) == set()
    assert "guardrail: FAILED" in stdout
    assert "guardrail: PASSED" in stdout

    # Six durable calls: the plan, four keyed lookups, the draft.
    assert len(set(source.completed_keys())) == 4
    assert source.count(EventType.TASK_COMPLETED) == 6

    # Every fork carries lineage back to the one source run, with the input overridden.
    for fork in forks:
        lineage = fork.payloads(EventType.RUN_FORKED)
        assert len(lineage) == 1
        assert lineage[0]["source_run_id"] == source.run_id
        assert lineage[0]["input_overridden"] is True

    # What each fork *executed*, as opposed to what it copied: a TaskAttemptStarted above
    # the RunForked marker means the executor entered the function body. Two forks cut
    # before `draft_reply` and re-ran exactly that one call; the third cut before
    # `plan_lookups` and re-ran the whole six. Asserted as a multiset because the store
    # lists runs by `created_at` and the demo's ManualClock stamps them all identically —
    # which fork is `@fork2` is not a promise (see `read_journal`).
    assert sorted(_executed_after_fork(fork) for fork in forks) == [
        ["draft_reply"],
        ["draft_reply"],
        ["plan_lookups", "look_up", "look_up", "look_up", "look_up", "draft_reply"],
    ]

    # And the reuse is byte-identical, which is the claim the demo makes in one line: the
    # fork that re-ran only the draft has the source's recorded outputs everywhere else.
    cheap = next(fork for fork in forks if _executed_after_fork(fork) == ["draft_reply"])
    before, after = _outputs(source), _outputs(cheap)
    assert set(before) == set(after), "the fork resolved a different set of durable calls"
    assert after["draft_reply:0"] != before["draft_reply:0"], "the re-run call did not change"
    reused = {identity for identity in before if identity != "draft_reply:0"}
    assert len(reused) == 5
    assert all(after[identity] == before[identity] for identity in reused)

    # The printed headline number, and the compare URL a reader will paste. The URL is
    # separately replayed against the real app by tests/e2e/test_example_urls.py.
    assert "1 of 6 durable calls re-ran; 5 were reused byte-identical" in stdout
    assert f"/runs/{source.run_id}/compare?to=" in stdout
    assert "identical — replayed" in stdout
    assert "DIFFERS  <- the fixed call" in stdout


def _executed_after_fork(facts: RunFacts) -> list[str]:
    """Task names whose body actually ran in this run, in journal order.

    A fork's journal opens with a verbatim copy of its source's prefix — attempt events
    included — so "did this run execute the call" is `seq > the RunForked marker`, not
    "is there a TaskAttemptStarted".
    """
    marker = next(e.seq for e in facts.events if e.type is EventType.RUN_FORKED)
    return [
        str(event.payload["task_name"])
        for event in facts.events
        if event.seq > marker and event.type is EventType.TASK_ATTEMPT_STARTED
    ]


def _outputs(facts: RunFacts) -> dict[str, Any]:
    """Recorded output per durable-call identity, as the compare view aligns them."""
    return {
        (
            f"{p['task_name']}:key:{p['key']}"
            if p.get("key") is not None
            else f"{p['task_name']}:{p.get('ordinal')}"
        ): p.get("output_ref")
        for p in facts.payloads(EventType.TASK_COMPLETED)
    }


# -- the V1 crash-recovery headline ----------------------------------------------


async def test_crash_recovery_example_reuses_the_recorded_step(tmp_path: Path) -> None:
    stdout = run_example("crash_recovery_demo.py", data_dir=tmp_path)
    runs = await read_journal(tmp_path)

    demo = runs["demo"]
    assert demo.status == RunStatus.COMPLETED.value
    assert demo.count(EventType.WORKFLOW_RESUMED) == 1  # one crash, one ⚡
    assert demo.count(EventType.TASK_COMPLETED) == 2  # each step recorded exactly once
    assert "REUSED, still 1" in stdout
