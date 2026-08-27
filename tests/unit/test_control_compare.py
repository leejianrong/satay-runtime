"""Unit tests: compare aligns two runs by durable-call identity (N16)."""

from __future__ import annotations

from satay.api.decorators import task, workflow
from satay.api.primitives import start
from satay.control.views import compare
from satay.journal.store import SQLiteStore


@task()
async def cmp_a(value: int) -> int:
    return value + 1


@task()
async def cmp_b(value: int) -> int:
    return value * 2


@task()
async def cmp_c(value: int) -> int:
    return value + 100


@workflow
async def cmp_wf(value: int) -> int:
    first = await cmp_a(value)
    return await cmp_b(first)


@workflow
async def cmp_wf_divergent(value: int) -> int:
    first = await cmp_a(value)
    return await cmp_c(first)


async def test_compare_aligns_shared_identities_across_two_runs() -> None:
    store = SQLiteStore.open(":memory:")
    a = start(cmp_wf, 1, store=store, run_id="run-a")
    b = start(cmp_wf, 10, store=store, run_id="run-b")
    await a.result()
    await b.result()

    result = await compare(store, "run-a", "run-b")
    rows = {row["identity"]: row for row in result["rows"]}

    assert set(rows) == {"cmp_a:0", "cmp_b:0"}
    assert rows["cmp_a:0"]["aligned"] is True
    assert rows["cmp_a:0"]["a"]["output"] == 2  # cmp_a(1)
    assert rows["cmp_a:0"]["b"]["output"] == 11  # cmp_a(10)
    assert rows["cmp_b:0"]["a"]["output"] == 4
    assert rows["cmp_b:0"]["b"]["output"] == 22
    assert result["a"]["run_id"] == "run-a"
    assert result["b"]["run_id"] == "run-b"
    store.close()


async def test_compare_marks_unaligned_identities_when_runs_diverge() -> None:
    store = SQLiteStore.open(":memory:")
    await start(cmp_wf, 1, store=store, run_id="orig").result()
    await start(cmp_wf_divergent, 1, store=store, run_id="fork").result()

    result = await compare(store, "orig", "fork")
    rows = {row["identity"]: row for row in result["rows"]}

    # The shared first task aligns; the diverging second task appears on one side only.
    assert rows["cmp_a:0"]["aligned"] is True
    assert rows["cmp_b:0"]["a"] is not None and rows["cmp_b:0"]["b"] is None
    assert rows["cmp_c:0"]["a"] is None and rows["cmp_c:0"]["b"] is not None
    assert rows["cmp_b:0"]["aligned"] is False
    store.close()


async def test_compare_rows_carry_a_structural_diff() -> None:
    """The compare view says *where* two sides differ, additively (ADR-0034/ADR-0018).

    Computed in the view, before ``ReadAPI`` redacts, so the paths survive masking of the
    values they point at. Timing is excluded from ``changed`` on purpose: duration varies
    between runs for reasons that are not a divergence.
    """
    store = SQLiteStore.open(":memory:")
    a = start(cmp_wf, 1, store=store, run_id="diff-a")
    b = start(cmp_wf, 10, store=store, run_id="diff-b")
    await a.result()
    await b.result()

    result = await compare(store, "diff-a", "diff-b")
    row = next(r for r in result["rows"] if r["identity"] == "cmp_a:0")

    assert row["diff"]["changed"] is True
    # cmp_a takes one positional argument, so the whole recorded argument list differs at
    # index 0, and its scalar output is not localisable to any field inside itself.
    assert row["diff"]["input"]["paths"] == ["[0]"]
    assert row["diff"]["output"]["paths"] == ["."]
    assert row["diff"]["attempts"] is False
    assert row["diff"]["input"]["redacted"] is False
    assert row["diff"]["input"]["truncated"] is False
    store.close()


async def test_compare_reports_no_change_for_a_run_against_itself() -> None:
    store = SQLiteStore.open(":memory:")
    handle = start(cmp_wf, 1, store=store, run_id="same-a")
    await handle.result()

    result = await compare(store, "same-a", "same-a")
    assert result["rows"]
    assert all(row["diff"]["changed"] is False for row in result["rows"])
    store.close()


async def test_an_unaligned_row_is_changed_with_nothing_to_diff() -> None:
    """An absent side is the difference, and there is no second value to compare."""
    store = SQLiteStore.open(":memory:")
    a = start(cmp_wf, 1, store=store, run_id="un-a")
    b = start(cmp_wf_divergent, 1, store=store, run_id="un-b")
    await a.result()
    await b.result()

    result = await compare(store, "un-a", "un-b")
    row = next(r for r in result["rows"] if r["identity"] == "cmp_c:0")
    assert row["aligned"] is False
    assert row["diff"]["changed"] is True
    assert row["diff"]["input"] is None
    assert row["diff"]["output"] is None
    store.close()
