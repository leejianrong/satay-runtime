"""E2E: the two ways a correctly-keyed side effect still runs twice (KAN-476).

Both traps are conceptual, both fail silently, and neither is a bug in the mechanics —
``INSERT OR IGNORE`` on ``ctx.idempotency_key`` works exactly as advertised in every test
here. What the tests pin is the *scope* of that key, and the runtime's new warning about
it:

* **Trap 1 — the key embeds the run id.** It deduplicates retries and resumes of one run.
  A second trigger of the same work mints a fresh ``run_id``, fresh keys, and a second
  copy of every row. The composition that closes it is ``satay.start(idempotency_key=)``
  **and** ``ctx.idempotency_key``, and
  :func:`test_the_documented_composition_survives_a_re_trigger` is the regression pin: it
  fails the moment a keyed start stops resolving to the same run.
* **Trap 2 — one key covers one call, not one row.** A four-row batch written under the
  bare key loads one row and reports success. Undetectable from inside the runtime, so
  the only guard is the docstring; the test below is the executable statement of it.

Observable outcomes only (ADR-0011): the rows in a real SQLite warehouse table with a
unique index, the run ids, and the ``satay`` logger.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

import satay
from satay.api.primitives import start
from satay.journal.store import SQLiteStore
from satay.testing.clock import ManualClock
from satay.testing.faults import FaultInjector, SimulatedCrash

#: The warehouse the "loads" in this module write to (one temp file per test).
WAREHOUSE: dict[str, Path] = {}

#: Records in one batch — four, because trap 2 is invisible with one.
RECORDS = ("r1", "r2", "r3", "r4")

#: Physical executions of the loader body, so "ran twice, wrote once" is provable.
BODIES: list[str] = []


def _connect() -> sqlite3.Connection:
    return sqlite3.connect(WAREHOUSE["path"])


def _insert_or_ignore(load_key: str, record_id: str) -> None:
    """One keyed write, exactly as a real loader would do it (unique index on load_key)."""
    conn = _connect()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO warehouse (load_key, record_id) VALUES (?, ?)",
            (load_key, record_id),
        )
        conn.commit()
    finally:
        conn.close()


def _rows() -> int:
    conn = _connect()
    try:
        return int(conn.execute("SELECT count(*) FROM warehouse").fetchone()[0])
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _warehouse(tmp_path: Path) -> None:
    """A fresh warehouse table (unique ``load_key``) and a fresh body counter per test."""
    WAREHOUSE["path"] = tmp_path / "warehouse.db"
    conn = _connect()
    try:
        conn.execute("CREATE TABLE warehouse (load_key TEXT PRIMARY KEY, record_id TEXT NOT NULL)")
        conn.commit()
    finally:
        conn.close()
    BODIES.clear()


# -- the workflows under test ----------------------------------------------------------


@satay.task(side_effect=True, retries=2, idempotent=True)
async def trap_load(batch: str) -> int:
    """A correct keyed loader: one composed dedupe key per row (the trap-2 fix)."""
    ctx = satay.task_context()
    BODIES.append(ctx.idempotency_key)
    for record_id in RECORDS:
        _insert_or_ignore(f"{ctx.idempotency_key}#{record_id}", record_id)
    return len(RECORDS)


@satay.workflow
async def trap_load_wf(batch: str) -> int:
    return await trap_load(batch)


@satay.task(side_effect=True, retries=2, idempotent=True)
async def trap_load_bare_key(batch: str) -> int:
    """The trap-2 loader: the bare call key as the unique column on a four-row batch."""
    ctx = satay.task_context()
    BODIES.append(ctx.idempotency_key)
    for record_id in RECORDS:
        _insert_or_ignore(ctx.idempotency_key, record_id)
    return len(RECORDS)


@satay.workflow
async def trap_load_bare_key_wf(batch: str) -> int:
    return await trap_load_bare_key(batch)


@satay.task(side_effect=True, retries=1, idempotent=True)
async def trap_load_lossy_ack(batch: str) -> int:
    """Writes its rows, then loses the acknowledgement on the first attempt."""
    ctx = satay.task_context()
    BODIES.append(ctx.idempotency_key)
    for record_id in RECORDS:
        _insert_or_ignore(f"{ctx.idempotency_key}#{record_id}", record_id)
    if ctx.attempt == 1:
        raise ConnectionError("the warehouse committed but the ack never came back")
    return len(RECORDS)


@satay.workflow
async def trap_load_lossy_ack_wf(batch: str) -> int:
    return await trap_load_lossy_ack(batch)


@satay.task()
async def trap_pure(value: int) -> int:
    """No declared side effect: outside effect safety's remit entirely."""
    return value + 1


@satay.workflow
async def trap_pure_wf(value: int) -> int:
    return await trap_pure(value)


@satay.workflow
async def trap_fanout_wf(batch: str) -> list[int]:
    """Five keyed side effects in one run — the warning must still be one line."""
    results: list[int] = await satay.map(trap_load, [f"{batch}-{n}" for n in range(5)], key=str)
    return results


@satay.workflow
async def trap_parent_wf(batch: str) -> int:
    """The effect lives in the child, which is where the re-trigger damage would land."""
    handle = await satay.start_child(trap_load_wf, batch)
    result: int = await handle.result()
    return result


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Warning lines about a keyed effect in a run nothing can name again."""
    return [r.getMessage() for r in caplog.records if "keys its side effect" in r.getMessage()]


# -- trap 1: the key embeds the run id -------------------------------------------------


async def test_two_unkeyed_triggers_double_load_the_same_batch(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The trap itself. Nothing here is misconfigured — and every row lands twice."""
    store = SQLiteStore.open(":memory:")
    with caplog.at_level(logging.WARNING, logger="satay"):
        first = start(trap_load_wf, "2026-08-16", store=store)
        assert await first.result() == 4
        assert _rows() == 4

        # The operator re-runs last night's load. New run id, new keys, second copy.
        second = start(trap_load_wf, "2026-08-16", store=store)
        assert await second.result() == 4

    assert second.run_id != first.run_id
    assert _rows() == 8  # four records, eight rows: the silent double load
    assert len(set(BODIES)) == 2  # two runs, two distinct ctx.idempotency_key values
    # ... and the runtime said so, before the second trigger ever happened.
    assert len(_warnings(caplog)) == 2
    assert "trap_load" in _warnings(caplog)[0]


async def test_the_documented_composition_survives_a_re_trigger(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The fix the docs prescribe: a keyed start **and** ``ctx.idempotency_key``.

    This is the regression pin for trap 1. It fails if a repeated start key stops
    resolving to the same run, or if the task key stops being derived from the run id.
    """
    store = SQLiteStore.open(":memory:")
    with caplog.at_level(logging.WARNING, logger="satay"):
        first = start(trap_load_wf, "2026-08-16", store=store, idempotency_key="load-2026-08-16")
        assert await first.result() == 4
        assert _rows() == 4

        second = start(trap_load_wf, "2026-08-16", store=store, idempotency_key="load-2026-08-16")
        assert await second.result() == 4

    assert second.run_id == first.run_id  # the repeat resolved to the same logical run
    assert _rows() == 4  # ... so the rows are still four, not eight
    assert len(BODIES) == 1  # the terminal run is a no-op: the body never re-ran
    assert _warnings(caplog) == []  # and the run was never flagged


async def test_the_composition_covers_the_retry_window_too(
    drain: Callable[..., Awaitable[Any]],
    manual_clock: ManualClock,
) -> None:
    """Both keys, both windows: the start key stops the re-trigger, ctx stops the retry.

    The loader commits its rows and then loses the ack, so the body runs twice inside one
    run (at-least-once). ``ctx.idempotency_key`` is what makes that harmless; the start
    key is what makes the *second trigger* harmless. Neither one covers the other's window,
    which is the whole reason the card asks for the composition.
    """
    store = SQLiteStore.open(":memory:")
    handle = start(
        trap_load_lossy_ack_wf, "b", store=store, clock=manual_clock, idempotency_key="lossy-1"
    )
    assert await drain(lambda: handle.result(), manual_clock) == 4

    assert len(BODIES) == 2  # two physical attempts of one logical call
    assert len(set(BODIES)) == 1  # ... on the same key
    assert _rows() == 4  # ... so four rows, not eight

    again = start(trap_load_lossy_ack_wf, "b", store=store, idempotency_key="lossy-1")
    assert await again.result() == 4
    assert len(BODIES) == 2  # unchanged: a terminal keyed run is a no-op
    assert _rows() == 4


async def test_a_crash_and_resume_of_a_keyed_run_still_loads_once(
    fault_injector: FaultInjector,
) -> None:
    """The resume window: same run id, same keys, one copy of the rows."""
    store = SQLiteStore.open(":memory:")
    fault_injector.crash_after("TaskCompleted")
    handle = start(
        trap_load_wf, "2026-08-16", store=store, injector=fault_injector, idempotency_key="nightly"
    )
    with pytest.raises(SimulatedCrash):
        await handle.result()
    assert _rows() == 4

    fault_injector.clear()
    resumed = start(trap_load_wf, "2026-08-16", store=store, idempotency_key="nightly")
    assert await resumed.result() == 4
    assert resumed.run_id == handle.run_id
    assert _rows() == 4  # the recorded completion was reused; no second write


# -- trap 2: one key covers one call, not one row --------------------------------------


async def test_the_bare_key_on_a_multi_row_effect_loads_one_row_of_four(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Trap 2, executable. The task reports success having written a quarter of the batch.

    The only difference from :func:`trap_load` is the missing ``#{record_id}`` on the
    dedupe key. Nothing in the runtime can see inside the effect, so nothing warns about
    this one — which is exactly why it is spelled out on ``ctx.idempotency_key`` instead.
    """
    store = SQLiteStore.open(":memory:")
    with caplog.at_level(logging.WARNING, logger="satay"):
        handle = start(trap_load_bare_key_wf, "b", store=store, idempotency_key="bare-1")
        assert await handle.result() == 4  # the task reports four rows written

    assert _rows() == 1  # ... and one row exists: rows 2-4 deduped against row 1
    assert _warnings(caplog) == []  # no warning is possible for this shape


async def test_the_composed_key_writes_every_row(caplog: pytest.LogCaptureFixture) -> None:
    """The control for the test above: ``f"{ctx.idempotency_key}#{record_id}"`` is the fix."""
    store = SQLiteStore.open(":memory:")
    with caplog.at_level(logging.WARNING, logger="satay"):
        handle = start(trap_load_wf, "b", store=store, idempotency_key="composed-1")
        assert await handle.result() == 4
    assert _rows() == 4


# -- the detector: fires on the shape, stays quiet on correct code ---------------------


async def test_the_warning_names_the_task_and_the_whole_fix(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The card's bar: following the warning must be enough to stop double-loading."""
    store = SQLiteStore.open(":memory:")
    with caplog.at_level(logging.WARNING, logger="satay"):
        await start(trap_load_wf, "b", store=store).result()

    (message,) = _warnings(caplog)
    assert "trap_load" in message
    assert "satay.start(..., idempotency_key=" in message  # the composition, spelled out
    assert "THIS run only" in message  # ... and why the ctx key is not enough alone
    assert "effect_safety='off'" in message  # ... and how to shut it up when it is wrong


async def test_a_keyed_start_is_not_flagged(caplog: pytest.LogCaptureFixture) -> None:
    """The correctness case: a run a re-trigger can resolve to is not at risk."""
    store = SQLiteStore.open(":memory:")
    with caplog.at_level(logging.WARNING, logger="satay"):
        await start(trap_load_wf, "b", store=store, idempotency_key="k").result()
    assert _warnings(caplog) == []


async def test_a_task_with_no_declared_side_effect_is_not_flagged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Only a declared ``side_effect=True, idempotent=True`` task can trip this."""
    store = SQLiteStore.open(":memory:")
    with caplog.at_level(logging.WARNING, logger="satay"):
        assert await start(trap_pure_wf, 1, store=store).result() == 2
    assert _warnings(caplog) == []


async def test_off_silences_it(caplog: pytest.LogCaptureFixture) -> None:
    """``effect_safety='off'`` is the escape hatch for a run known to be one-shot."""
    store = SQLiteStore.open(":memory:")
    with caplog.at_level(logging.WARNING, logger="satay"):
        await start(trap_load_wf, "b", store=store, effect_safety="off").result()
    assert _warnings(caplog) == []


async def test_strict_warns_but_does_not_raise(caplog: pytest.LogCaptureFixture) -> None:
    """Deliberate: the condition is a risk, not a defect, so it never escalates.

    A one-shot run genuinely has no start-level key and is perfectly correct. Raising
    ``EffectSafetyError`` on a guess would break those programs under ``strict``, so
    ``strict`` gets the same warning ``warn`` does — and the run completes.
    """
    store = SQLiteStore.open(":memory:")
    with caplog.at_level(logging.WARNING, logger="satay"):
        assert await start(trap_load_wf, "b", store=store, effect_safety="strict").result() == 4
    assert len(_warnings(caplog)) == 1
    assert _rows() == 4  # it ran; nothing was rejected


async def test_a_fan_out_warns_once_not_once_per_item(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Noise control: five keyed items, one line. A warning per item would be ignored."""
    store = SQLiteStore.open(":memory:")
    with caplog.at_level(logging.WARNING, logger="satay"):
        assert await start(trap_fanout_wf, "b", store=store).result() == [4, 4, 4, 4, 4]
    assert len(_warnings(caplog)) == 1
    assert _rows() == 20  # five items, four records each, each under its own map key


async def test_a_child_of_a_keyed_parent_is_not_flagged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A child run id is minted by the parent, so it is as re-derivable as the parent."""
    store = SQLiteStore.open(":memory:")
    with caplog.at_level(logging.WARNING, logger="satay"):
        handle = start(trap_parent_wf, "b", store=store, idempotency_key="parent-1")
        assert await handle.result() == 4
    assert _warnings(caplog) == []


async def test_a_child_of_an_unkeyed_parent_is_flagged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The other direction: re-trigger the parent and the child's effect lands again."""
    store = SQLiteStore.open(":memory:")
    with caplog.at_level(logging.WARNING, logger="satay"):
        assert await start(trap_parent_wf, "b", store=store).result() == 4
    assert len(_warnings(caplog)) == 1
