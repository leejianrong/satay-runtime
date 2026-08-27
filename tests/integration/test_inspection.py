"""``satay.inspect``: reading a run's recorded durable calls without forking (KAN-477).

Driven through the public API against a temp SQLite store, asserting on observable
outcomes only (ADR-0011). The point of the card is that reading is a *read*: no new run
row, no journal append, no re-execution — so several of these tests assert what does
**not** change as much as what comes back.
"""

from __future__ import annotations

import pytest

import satay
from satay.control import views
from satay.journal.events import RunStatus
from satay.journal.store import SQLiteStore
from satay.redaction import REDACTED, Redactor


@satay.task()
async def insp_add(a: int, b: int) -> int:
    return a + b


@satay.task()
async def insp_upper(text: str) -> str:
    return text.upper()


@satay.task()
async def insp_resize(path: str) -> str:
    return f"resized:{path}"


@satay.workflow
async def insp_two_steps(value: int) -> str:
    total = await insp_add(value, 10)
    return await insp_upper(f"total-{total}")


@satay.workflow
async def insp_fan_out(paths: list[str]) -> list[str]:
    return await satay.map(insp_resize, paths, key=lambda p: p)


@satay.task(retries=1)
async def insp_boom(value: int) -> int:
    raise ValueError("kaboom")


@satay.workflow
async def insp_failing(value: int) -> int:
    return await insp_boom(value)


async def test_inspect_returns_every_call_in_schedule_order() -> None:
    """The headline: what the run recorded, in the order it happened."""
    store = SQLiteStore.open(":memory:")
    handle = satay.start(insp_two_steps, 5, store=store)
    assert await handle.result() == "TOTAL-15"

    inspection = await satay.inspect(handle.run_id, store=store)

    assert inspection.run_id == handle.run_id
    assert inspection.workflow_name == "insp_two_steps"
    assert inspection.status is RunStatus.COMPLETED
    assert inspection.output == "TOTAL-15"
    assert inspection.error is None

    assert [call.identity for call in inspection.calls] == ["insp_add:0", "insp_upper:0"]
    added, uppered = inspection.calls
    assert added.args == (5, 10)
    assert added.output == 15
    assert added.status == "completed"
    assert added.attempts == 1
    assert added.ordinal == 0
    assert added.key is None
    assert uppered.args == ("total-15",)
    assert uppered.output == "TOTAL-15"
    store.close()


@satay.workflow
async def insp_reverse_alphabetical(value: int) -> int:
    """Schedules ``insp_upper`` before ``insp_add`` so schedule order != sorted order."""
    text = await insp_upper(f"n{value}")
    return await insp_add(len(text), 1)


async def test_calls_are_ordered_by_schedule_not_by_identity() -> None:
    """Schedule order is the contract, and it is *not* what ``compare`` returns.

    ``views.compare`` sorts its rows alphabetically by identity for stable two-run
    alignment. A reader following what a run did wants the sequence it happened in, so
    this pins the difference rather than inheriting the compare ordering by accident.

    The workflow schedules in reverse-alphabetical order deliberately: with a workflow
    whose call names happen to sort the way it runs, this test would pass while proving
    nothing, so the second assertion guards the first.
    """
    store = SQLiteStore.open(":memory:")
    handle = satay.start(insp_reverse_alphabetical, 5, store=store)
    await handle.result()

    identities = [call.identity for call in (await satay.inspect(handle.run_id, store=store)).calls]
    assert identities == ["insp_upper:0", "insp_add:0"]
    assert identities != sorted(identities), "the test workflow no longer proves the point"
    store.close()


async def test_inspect_reports_keyed_fan_out_items() -> None:
    """A ``map`` member identifies by key, not ordinal, and names its group (ADR-0002)."""
    store = SQLiteStore.open(":memory:")
    handle = satay.start(insp_fan_out, ["a.png", "b.png"], store=store)
    await handle.result()

    inspection = await satay.inspect(handle.run_id, store=store)
    assert [call.identity for call in inspection.calls] == [
        "insp_resize:key:a.png",
        "insp_resize:key:b.png",
    ]
    first = inspection.calls[0]
    assert first.key == "a.png"
    assert first.ordinal is None
    assert first.map_group == "map:0:insp_resize"
    assert first.output == "resized:a.png"
    store.close()


async def test_inspect_reports_a_failure_instead_of_raising_it() -> None:
    """A read that raises the failure it was asked about is harder to use, not safer.

    ``await handle.result()`` raises ``WorkflowFailedError`` — correct for driving a run,
    wrong for inspecting one. The failed call is still reported as ``failed`` even though
    a fail-fast task failure appends no per-task terminal event: the status is inferred
    from the run's own failure.
    """
    store = SQLiteStore.open(":memory:")
    handle = satay.start(insp_failing, 1, store=store)
    with pytest.raises(satay.WorkflowFailedError):
        await handle.result()

    inspection = await satay.inspect(handle.run_id, store=store)
    assert inspection.status is RunStatus.FAILED
    assert inspection.output is None
    assert inspection.error is not None
    assert inspection.error["type"] == "ValueError"
    assert "kaboom" in inspection.error["message"]

    (failed,) = inspection.calls
    assert failed.status == "failed"
    assert failed.attempts == 2  # one retry
    store.close()


async def test_inspect_neither_writes_nor_re_executes() -> None:
    """The whole reason the card exists: fork paid a write and a re-drive for a read."""
    store = SQLiteStore.open(":memory:")
    executions = 0

    @satay.task()
    async def insp_counted(value: int) -> int:
        nonlocal executions
        executions += 1
        return value

    @satay.workflow
    async def insp_counts(value: int) -> int:
        return await insp_counted(value)

    handle = satay.start(insp_counts, 7, store=store)
    await handle.result()
    assert executions == 1

    runs_before = len(await store.list_runs())
    events_before = await store.read_events(handle.run_id)

    await satay.inspect(handle.run_id, store=store)
    await satay.inspect(handle.run_id, store=store)

    assert executions == 1, "inspect re-executed a recorded call"
    assert len(await store.list_runs()) == runs_before, "inspect created a run"
    assert await store.read_events(handle.run_id) == events_before, "inspect wrote an event"
    store.close()


async def test_inspect_works_on_an_unfinished_run() -> None:
    """A read is not a fork, so ADR-0004's terminal-only rule does not apply here."""
    store = SQLiteStore.open(":memory:")

    @satay.workflow
    async def insp_parks(value: int) -> int:
        await insp_add(value, 1)
        await satay.sleep(300)
        return value

    handle = satay.start(insp_parks, 1, store=store)
    assert await handle.result() is satay.PARKED

    inspection = await satay.inspect(handle.run_id, store=store)
    assert inspection.status is RunStatus.WAITING
    assert inspection.output is None
    assert [call.identity for call in inspection.calls] == ["insp_add:0"]
    assert inspection.calls[0].output == 2
    store.close()


async def test_unknown_run_raises_lookup_error() -> None:
    """Catchable without importing anything out of ``satay.control``."""
    store = SQLiteStore.open(":memory:")
    with pytest.raises(LookupError):
        await satay.inspect("no-such-run", store=store)
    store.close()


async def test_call_lookup_by_identity() -> None:
    store = SQLiteStore.open(":memory:")
    handle = satay.start(insp_two_steps, 5, store=store)
    await handle.result()

    inspection = await satay.inspect(handle.run_id, store=store)
    found = inspection.call("insp_add:0")
    assert found is not None
    assert found.output == 15
    assert inspection.call("insp_add:99") is None
    store.close()


# --- redaction (ADR-0009 N18) ---------------------------------------------------------


@satay.task()
async def insp_issues_token(user: str) -> dict[str, str]:
    return {"session_token": "sk-live-secret", "user": user}


@satay.workflow
async def insp_leaks(user: str) -> dict[str, str]:
    return await insp_issues_token(user)


async def test_inspect_redacts_by_default_and_the_builder_does_not() -> None:
    """There is no unredacted read path, and redaction is the read-time transform.

    The second half matters as much as the first: asserting only that ``inspect`` masks
    the secret would pass just as well if the value had never been stored, which would be
    a different (and wrong) design. So this proves the raw builder still holds it.
    """
    store = SQLiteStore.open(":memory:")
    handle = satay.start(insp_leaks, "ada", store=store)
    await handle.result()

    inspection = await satay.inspect(handle.run_id, store=store)
    (call,) = inspection.calls
    assert call.output["session_token"] == REDACTED
    assert call.output["user"] == "ada"
    assert inspection.output["session_token"] == REDACTED

    raw = await views.run_calls(store, handle.run_id)
    assert raw["calls"][0]["output"]["session_token"] == "sk-live-secret"
    store.close()


async def test_a_caller_supplied_redactor_replaces_the_default() -> None:
    store = SQLiteStore.open(":memory:")
    handle = satay.start(insp_leaks, "ada", store=store)
    await handle.result()

    inspection = await satay.inspect(
        handle.run_id, store=store, redactor=Redactor(patterns=["user"])
    )
    (call,) = inspection.calls
    assert call.output["user"] == REDACTED
    assert call.output["session_token"] == "sk-live-secret"
    store.close()


@satay.task()
async def insp_fetch_secret(name: str) -> str:
    return f"value-of-{name}"


@satay.workflow
async def insp_named_like_a_pattern(name: str) -> str:
    return await insp_fetch_secret(name)


async def test_a_task_named_like_a_redaction_pattern_is_still_readable() -> None:
    """Regression: the redactor matches field *names*, so identity must not be a key.

    ``Redactor.matches`` is a case-insensitive substring test, so a task named
    ``insp_fetch_secret`` matches the default ``secret`` pattern. An earlier draft keyed
    the calls view by identity, which meant the redactor masked that call's **entire**
    record — task name, args, output, attempts — and the caller got a bare
    ``"***REDACTED***"`` string where a call was expected. Carrying ``identity`` as a
    field instead keeps the structure intact and redacts only genuinely matching fields.
    """
    store = SQLiteStore.open(":memory:")
    handle = satay.start(insp_named_like_a_pattern, "db-password", store=store)
    await handle.result()

    inspection = await satay.inspect(handle.run_id, store=store)
    (call,) = inspection.calls
    assert call.identity == "insp_fetch_secret:0"
    assert call.task_name == "insp_fetch_secret"
    assert call.status == "completed"
    assert call.output == "value-of-db-password"
    store.close()


# --- child workflows (ADR-0027 / V4 linkage) ------------------------------------------


@satay.workflow
async def insp_child(value: int) -> int:
    return await insp_add(value, 100)


@satay.workflow
async def insp_parent(value: int) -> dict[str, object]:
    child = await satay.start_child(insp_child, value)
    tail = await insp_upper("tail")
    return {"child": await child.result(), "tail": tail}


async def test_child_workflow_calls_are_included_in_schedule_order() -> None:
    """A ``start_child`` call is a durable call, and it is not a ``TASK_*`` event.

    ``_scan_tasks`` only sees the four task-lifecycle events, so a read built on it alone
    omits child workflows entirely — silently, and for one of the five primitives. This
    pins that they appear, in the order the parent scheduled them, with the child's own
    recorded output (which lives in the child's journal, not the parent's).
    """
    store = SQLiteStore.open(":memory:")
    handle = satay.start(insp_parent, 5, store=store)
    await handle.result()

    inspection = await satay.inspect(handle.run_id, store=store)
    child_call, tail_call = inspection.calls

    assert child_call.child_run_id is not None
    assert child_call.task_name == "insp_child"
    assert child_call.status == "completed"
    assert child_call.args == (5,)
    assert child_call.output == 105, "the child's output must come from the child's journal"

    assert tail_call.identity == "insp_upper:0"
    assert tail_call.child_run_id is None
    store.close()


async def test_a_child_run_id_can_be_inspected_in_turn() -> None:
    """``child_run_id`` is the handle for walking down into the child's own calls."""
    store = SQLiteStore.open(":memory:")
    handle = satay.start(insp_parent, 5, store=store)
    await handle.result()

    parent_inspection = await satay.inspect(handle.run_id, store=store)
    child_run_id = next(c.child_run_id for c in parent_inspection.calls if c.child_run_id)

    child_inspection = await satay.inspect(child_run_id, store=store)
    assert child_inspection.workflow_name == "insp_child"
    assert child_inspection.output == 105
    assert [c.identity for c in child_inspection.calls] == ["insp_add:0"]
    assert child_inspection.calls[0].args == (5, 100)
    store.close()


@satay.workflow
async def insp_sums(values: list[int]) -> int:
    return await insp_add(sum(values), 0)


@satay.workflow
async def insp_parent_with_list_input(values: list[int]) -> int:
    child = await satay.start_child(insp_sums, values)
    return await child.result()


async def test_a_child_whose_input_is_a_list_reads_back_as_one_argument() -> None:
    """A child takes exactly one input, even when that input is itself a list.

    A task's ``input_ref`` holds ``encode(list(args))`` while a child's holds the single
    input value, so treating the two identically would unpack a list-valued child input
    into N arguments — reporting ``args == (1, 2, 3)`` for a call that received one list.
    """
    store = SQLiteStore.open(":memory:")
    handle = satay.start(insp_parent_with_list_input, [1, 2, 3], store=store)
    assert await handle.result() == 6

    inspection = await satay.inspect(handle.run_id, store=store)
    (child_call,) = inspection.calls
    assert child_call.args == ([1, 2, 3],)
    store.close()


async def test_blob_spilled_output_is_resolved_transparently() -> None:
    """A payload over the spill threshold lives in a blob file; the read resolves it."""
    store = SQLiteStore.open(":memory:")

    @satay.task()
    async def insp_bulky(size: int) -> str:
        return "x" * size

    @satay.workflow
    async def insp_spills(size: int) -> int:
        return len(await insp_bulky(size))

    handle = satay.start(insp_spills, 300_000, store=store)
    await handle.result()

    inspection = await satay.inspect(handle.run_id, store=store)
    (call,) = inspection.calls
    assert isinstance(call.output, str)
    assert len(call.output) == 300_000
    store.close()
