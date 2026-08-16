"""Fork from code: ``before_task=`` selection and the ``workflow_input=`` override.

KAN-481 / ADR-0028. Driven through the public seam (ADR-0011): ``satay.fork`` and
``satay.start`` against a temp ``SQLiteStore``, asserting observable outcomes — the
fork's result, its status, its journal, and per-task execution counts — never replay
internals. The workflows here take their prompt *in the input*, which is the whole
point of the card: expressing "re-run this with a sharper prompt" must not require
module-global mutable state.
"""

from __future__ import annotations

import pytest

import satay
from satay.api.decorators import task, workflow
from satay.api.primitives import start
from satay.control.api import ControlAPI
from satay.control.commands import CommandQueue, ForkValidationError
from satay.journal.codec import decode
from satay.journal.events import EventType, RunStatus
from satay.journal.store import SQLiteStore
from satay.timers import TimerEventWorker

EXECUTIONS: dict[str, int] = {}


def _count(name: str) -> int:
    return EXECUTIONS.get(name, 0)


@pytest.fixture(autouse=True)
def _reset() -> None:
    EXECUTIONS.clear()


@task()
async def k481_research(topic: str) -> str:
    EXECUTIONS["k481_research"] = _count("k481_research") + 1
    return f"notes on {topic}"


@task()
async def k481_synthesize(brief: dict[str, str]) -> str:
    EXECUTIONS["k481_synthesize"] = _count("k481_synthesize") + 1
    return f"{brief['style']}: {brief['notes']}"


@workflow
async def k481_dossier(brief: dict[str, str]) -> str:
    """The card's shape: the prompt lives in the brief, not in a module global."""
    notes = await k481_research(brief["topic"])
    return await k481_synthesize({"style": brief["style"], "notes": notes})


@task()
async def k481_step(value: int) -> int:
    EXECUTIONS["k481_step"] = _count("k481_step") + 1
    return value + 1


@workflow
async def k481_thrice(value: int) -> int:
    """The same task three times — the ambiguous ``before_task`` case."""
    first = await k481_step(value)
    second = await k481_step(first)
    return await k481_step(second)


@task()
async def k481_polish(text: str) -> str:
    EXECUTIONS["k481_polish"] = _count("k481_polish") + 1
    return f"{text} (polished)"


@task()
async def k481_shorten(text: str) -> str:
    EXECUTIONS["k481_shorten"] = _count("k481_shorten") + 1
    return text.split()[0]


@workflow
async def k481_branchy(brief: dict[str, str]) -> str:
    """The input decides *which tasks run*, so a changed input can diverge the prefix."""
    notes = await k481_research(brief["topic"])
    if brief["mode"] == "polish":
        return await k481_polish(notes)
    return await k481_shorten(notes)


def _fork_lineage(events: object) -> dict[str, object]:
    forked = [
        e
        for e in events  # type: ignore[attr-defined]
        if e.type is EventType.RUN_FORKED
    ]
    payload: dict[str, object] = dict(forked[-1].payload)
    return payload


# -- before_task= --------------------------------------------------------------------


async def test_before_task_cuts_so_that_task_reruns_and_the_prefix_is_reused() -> None:
    """The headline: no journal archaeology — name the task, get the right prefix."""
    store = SQLiteStore.open(":memory:")
    brief = {"topic": "satay", "style": "terse"}
    assert await start(k481_dossier, brief, store=store, run_id="src").result() == (
        "terse: notes on satay"
    )

    handle = await satay.fork("src", before_task="k481_synthesize", store=store)
    assert await handle.result() == "terse: notes on satay"

    # research was a journal hit (not re-run); synthesize re-ran after the fork point.
    assert _count("k481_research") == 1
    assert _count("k481_synthesize") == 2
    record = await store.get_run(handle.run_id)
    assert record is not None and record.status is RunStatus.COMPLETED
    # A fresh fork is a new run, not a crash recovery: no WorkflowResumed, so no ⚡.
    fk_events = await store.read_events(handle.run_id)
    assert not [e for e in fk_events if e.type is EventType.WORKFLOW_RESUMED]
    assert _fork_lineage(fk_events)["source_run_id"] == "src"
    store.close()


async def test_before_task_selects_the_earliest_of_several_occurrences() -> None:
    """A repeated task name is not an error: the *earliest* occurrence is the cut."""
    store = SQLiteStore.open(":memory:")
    assert await start(k481_thrice, 0, store=store, run_id="src").result() == 3
    assert _count("k481_step") == 3

    handle = await satay.fork("src", before_task="k481_step", store=store)
    assert await handle.result() == 3
    # Cutting before the first occurrence re-runs all three (3 source + 3 fork).
    assert _count("k481_step") == 6
    store.close()


async def test_before_ordinal_selects_a_named_occurrence() -> None:
    """``before_ordinal=`` names the occurrence by the ordinal Studio/compare show."""
    store = SQLiteStore.open(":memory:")
    assert await start(k481_thrice, 0, store=store, run_id="src").result() == 3

    handle = await satay.fork("src", before_task="k481_step", before_ordinal=2, store=store)
    assert await handle.result() == 3
    # Ordinals 0 and 1 were reused as journal hits; only the third call re-ran.
    assert _count("k481_step") == 4
    store.close()


async def test_before_task_naming_a_task_that_never_ran_lists_the_ones_that_did() -> None:
    store = SQLiteStore.open(":memory:")
    await start(k481_dossier, {"topic": "t", "style": "s"}, store=store, run_id="src").result()

    with pytest.raises(ForkValidationError) as excinfo:
        await satay.fork("src", before_task="k481_typo", store=store)
    message = str(excinfo.value)
    assert "k481_typo" in message
    assert "k481_research" in message and "k481_synthesize" in message
    # Nothing was created: a rejected fork leaves no half-seeded run behind.
    assert len(await store.list_runs()) == 1
    store.close()


async def test_before_ordinal_out_of_range_names_the_ordinals_that_exist() -> None:
    store = SQLiteStore.open(":memory:")
    await start(k481_thrice, 0, store=store, run_id="src").result()

    with pytest.raises(ForkValidationError) as excinfo:
        await satay.fork("src", before_task="k481_step", before_ordinal=9, store=store)
    assert "ordinals 0, 1, 2" in str(excinfo.value)
    store.close()


async def test_fork_point_and_before_task_are_mutually_exclusive() -> None:
    store = SQLiteStore.open(":memory:")
    await start(k481_thrice, 0, store=store, run_id="src").result()

    with pytest.raises(ForkValidationError):
        await satay.fork("src", store=store)  # neither
    with pytest.raises(ForkValidationError):
        await satay.fork("src", before_task="k481_step", fork_point_seq=1, store=store)  # both
    with pytest.raises(ForkValidationError):
        await satay.fork("src", before_ordinal=1, store=store)  # ordinal without a name
    store.close()


async def test_control_api_fork_accepts_before_task_too() -> None:
    """The HTTP-side write facade resolves the same way (one resolver, two callers)."""
    store = SQLiteStore.open(":memory:")
    queue = CommandQueue()
    control = ControlAPI(store, queue)
    worker = TimerEventWorker(store=store, commands=queue)
    await start(k481_dossier, {"topic": "t", "style": "s"}, store=store, run_id="src").result()

    new_id = await control.fork("src", before_task="k481_synthesize")
    await worker.tick()

    record = await store.get_run(new_id)
    assert record is not None and record.status is RunStatus.COMPLETED
    assert _count("k481_research") == 1  # reused across the fork
    assert _count("k481_synthesize") == 2
    store.close()


# -- workflow_input= -----------------------------------------------------------------


async def test_workflow_input_override_changes_the_result_without_re_running_the_prefix() -> None:
    """The V7 user story: same run, sharper prompt, only the suffix re-cut."""
    store = SQLiteStore.open(":memory:")
    await start(
        k481_dossier, {"topic": "satay", "style": "terse"}, store=store, run_id="src"
    ).result()

    handle = await satay.fork(
        "src",
        before_task="k481_synthesize",
        workflow_input={"topic": "satay", "style": "lyrical"},
        store=store,
    )
    assert await handle.result() == "lyrical: notes on satay"

    # The expensive upstream call was NOT paid for again; only synthesize re-ran.
    assert _count("k481_research") == 1
    assert _count("k481_synthesize") == 2

    # The override is recorded in the fork's own journal (durable, not drive-time only).
    fk_events = await store.read_events(handle.run_id)
    created = next(e for e in fk_events if e.type is EventType.WORKFLOW_CREATED)
    assert decode(created.payload["input_ref"])["style"] == "lyrical"
    lineage = _fork_lineage(fk_events)
    assert lineage["input_overridden"] is True
    assert decode(lineage["source_input_ref"])["style"] == "terse"  # type: ignore[arg-type]

    # The source run is untouched and still returns its original answer.
    src_created = next(
        e for e in await store.read_events("src") if e.type is EventType.WORKFLOW_CREATED
    )
    assert decode(src_created.payload["input_ref"])["style"] == "terse"
    store.close()


async def test_an_overridden_input_survives_a_reread_of_the_fork() -> None:
    """The fork's recorded input is its own: re-reading it does not revert to the source."""
    store = SQLiteStore.open(":memory:")
    await start(
        k481_dossier, {"topic": "satay", "style": "terse"}, store=store, run_id="src"
    ).result()
    handle = await satay.fork(
        "src",
        before_task="k481_synthesize",
        workflow_input={"topic": "satay", "style": "lyrical"},
        store=store,
    )
    assert await handle.result() == "lyrical: notes on satay"

    # A later reader that knows nothing about the override reads the recorded outcome.
    again = await start(k481_dossier, None, run_id=handle.run_id, store=store).result()
    assert again == "lyrical: notes on satay"
    store.close()


async def test_workflow_input_none_is_a_real_override_not_inherit() -> None:
    """``None`` is a valid input, so the sentinel — not ``None`` — means 'inherit'."""
    store = SQLiteStore.open(":memory:")
    await start(k481_dossier, {"topic": "t", "style": "s"}, store=store, run_id="src").result()

    handle = await satay.fork("src", before_task="k481_research", workflow_input=None, store=store)
    with pytest.raises(satay.WorkflowFailedError):  # brief["topic"] on None
        await handle.result()
    record = await store.get_run(handle.run_id)
    assert record is not None and record.status is RunStatus.FAILED
    store.close()


# -- workflow_input= x nondeterminism (ADR-0028) -------------------------------------


async def test_an_override_that_diverges_the_copied_prefix_raises_under_strict() -> None:
    """Strict stays strict: splicing two incompatible histories is refused (ADR-0022)."""
    store = SQLiteStore.open(":memory:")
    await start(
        k481_branchy, {"topic": "satay", "mode": "polish"}, store=store, run_id="src"
    ).result()

    # Fork *after* the branch, then change the branch: position 1 recorded k481_polish
    # but the new input issues k481_shorten there.
    after_branch = max(
        e.seq
        for e in await store.read_events("src")
        if e.type is EventType.TASK_COMPLETED and e.payload["task_name"] == "k481_polish"
    )
    handle = await satay.fork(
        "src",
        fork_point_seq=after_branch,
        workflow_input={"topic": "satay", "mode": "shorten"},
        store=store,
    )
    with pytest.raises(satay.NondeterminismError) as excinfo:
        await handle.result()
    assert "k481_polish" in str(excinfo.value) and "k481_shorten" in str(excinfo.value)
    # The divergent call never executed and nothing terminal was recorded.
    assert _count("k481_shorten") == 0
    assert not [
        e for e in await store.read_events(handle.run_id) if e.type is EventType.WORKFLOW_COMPLETED
    ]
    store.close()


async def test_forking_before_the_divergence_is_the_fix_and_it_succeeds() -> None:
    """The remedy the error implies: cut before the first call the new input changes."""
    store = SQLiteStore.open(":memory:")
    await start(
        k481_branchy, {"topic": "satay", "mode": "polish"}, store=store, run_id="src"
    ).result()

    handle = await satay.fork(
        "src",
        before_task="k481_polish",
        workflow_input={"topic": "satay", "mode": "shorten"},
        store=store,
    )
    assert await handle.result() == "notes"  # k481_shorten("notes on satay")
    assert _count("k481_research") == 1  # still reused: the prefix before the branch
    assert _count("k481_shorten") == 1
    store.close()


async def test_an_override_past_the_terminal_event_is_refused_not_silently_ignored() -> None:
    """Copying a whole finished run re-executes nothing, so the override would be a lie."""
    store = SQLiteStore.open(":memory:")
    await start(
        k481_dossier, {"topic": "satay", "style": "terse"}, store=store, run_id="src"
    ).result()

    with pytest.raises(ForkValidationError) as excinfo:
        await satay.fork(
            "src",
            fork_point_seq=max(e.seq for e in await store.read_events("src")),
            workflow_input={"topic": "satay", "style": "lyrical"},
            store=store,
        )
    assert "would have no effect" in str(excinfo.value)
    assert len(await store.list_runs()) == 1  # nothing half-created
    store.close()


async def test_an_override_that_only_changes_prefix_arguments_reuses_the_prefix() -> None:
    """Documented and deliberate: the prefix is history, so the new topic reaches nothing.

    Detection compares the durable-call *schedule*, not arguments (ADR-0003/0022), and
    a fork's prefix is by construction "what already happened". Changing ``topic`` while
    forking after research therefore keeps the old notes — which is why ADR-0028's rule
    is *put the fork point before the first call that should see the new input*.
    """
    store = SQLiteStore.open(":memory:")
    await start(
        k481_dossier, {"topic": "satay", "style": "terse"}, store=store, run_id="src"
    ).result()

    handle = await satay.fork(
        "src",
        before_task="k481_synthesize",
        workflow_input={"topic": "rendang", "style": "terse"},
        store=store,
    )
    assert await handle.result() == "terse: notes on satay"  # NOT "notes on rendang"
    assert _count("k481_research") == 1

    # Cutting before research instead does pick the new topic up.
    handle2 = await satay.fork(
        "src",
        before_task="k481_research",
        workflow_input={"topic": "rendang", "style": "terse"},
        store=store,
    )
    assert await handle2.result() == "terse: notes on rendang"
    store.close()
