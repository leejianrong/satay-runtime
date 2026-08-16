"""End-to-end: ``satay.run_app`` drives a parked run to completion (KAN-491, ADR-0030).

The capability under test is the one the docs had to fake: a script that uses
``satay.sleep`` or ``wait_for_event`` needs a poll loop, and until now the only supported
way to get one was ``satay dev``, which lives in the optional extra. Everything here runs
on a plain ``pip install satay`` — the whole point — against a real SQLite journal, and
asserts only what a user can see: the returned result, the run status, and whether the
loop is still there afterwards.

Real time, deliberately. A ``ManualClock`` would freeze the very background loop being
tested (ADR-0030), so the sleeps are short instead of virtual: this module's whole budget
is well under a second of wall clock.
"""

from __future__ import annotations

import asyncio
import copy
import pickle
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest

import satay
from satay.journal.store import SQLiteStore
from satay.timers import poll_loop_running

#: Short enough to keep the suite fast, long enough that the run really does park before
#: the poll loop gets to it.
_NAP_SECONDS = 0.05

#: Poll cadence for the worker under test — fast, since nothing here waits on real work.
_INTERVAL = 0.01


@dataclass(frozen=True)
class RunAppApproval:
    """An external event this module sends from outside the workflow."""

    approved: bool


@satay.task()
async def _ra_double(value: int) -> int:
    return value * 2


@satay.workflow
async def ra_naps(value: int) -> int:
    """Parks on a durable timer, then finishes."""
    doubled = await _ra_double(value)
    await satay.sleep(_NAP_SECONDS)
    return doubled + 1


@satay.workflow
async def ra_waits(value: int) -> str:
    """Parks on an event wait, then finishes."""
    decision = await satay.wait_for_event(RunAppApproval, key="run-app", timeout=30)
    if decision is None:
        return "timed out"
    return "approved" if decision.approved else "rejected"


@satay.workflow
async def ra_returns_none(value: int) -> None:
    """A workflow whose real, terminal result is ``None`` — the ambiguity PARKED fixes."""
    await _ra_double(value)
    return None


async def test_a_run_that_parks_on_a_timer_completes_inside_run_app(tmp_path: Path) -> None:
    """The headline: one ``async with``, and a sleeping workflow returns its result."""
    async with satay.run_app(data_dir=tmp_path / ".satay", interval=_INTERVAL) as store:
        handle = satay.start(ra_naps, 10, store=store)
        assert await handle.result() == 21
        assert await handle.status() == "completed"


async def test_a_run_that_parks_on_an_event_completes_inside_run_app(tmp_path: Path) -> None:
    """Same for an event wait: send it, then await the handle — no polling by hand."""
    async with satay.run_app(data_dir=tmp_path / ".satay", interval=_INTERVAL) as store:
        handle = satay.start(ra_waits, 0, store=store)
        await satay.send_event(RunAppApproval(approved=True), key="run-app", store=store)
        assert await handle.result() == "approved"
        assert await handle.status() == "completed"


async def test_run_app_opens_the_journal_where_it_says_it_does(tmp_path: Path) -> None:
    """``data_dir=`` is honoured, and the run really is on disk afterwards."""
    data_dir = tmp_path / ".satay"
    async with satay.run_app(data_dir=data_dir, interval=_INTERVAL) as store:
        run_id = satay.start(ra_naps, 1, store=store).run_id
        assert await satay.start(ra_naps, 1, run_id=run_id, store=store).result() == 3

    reopened = SQLiteStore.open(data_dir / "satay.db")
    try:
        record = await reopened.get_run(run_id)
        assert record is not None
        assert record.status.value == "completed"
    finally:
        reopened.close()


async def test_teardown_happens_even_when_the_body_raises(tmp_path: Path) -> None:
    """The ``try``/``finally`` the reader used to write is now the runtime's problem."""
    captured: list[SQLiteStore] = []

    with pytest.raises(RuntimeError, match="boom"):
        async with satay.run_app(data_dir=tmp_path / ".satay", interval=_INTERVAL) as store:
            assert isinstance(store, SQLiteStore)
            captured.append(store)
            assert poll_loop_running(store)
            raise RuntimeError("boom")

    opened = captured[0]
    assert not poll_loop_running(opened), "the poll loop outlived the block"
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        # The store it opened is closed: a read through it no longer works.
        await opened.list_runs()


async def test_a_caller_supplied_store_is_left_open(tmp_path: Path) -> None:
    """``store=`` means the caller owns the lifetime; only the loop is ours."""
    store = SQLiteStore.open(":memory:")
    try:
        async with satay.run_app(store=store, interval=_INTERVAL):
            assert await satay.start(ra_naps, 2, store=store).result() == 5
        assert not poll_loop_running(store)
        assert await store.list_runs()  # still usable: we did not close it
    finally:
        store.close()


async def test_data_dir_and_store_together_are_a_usage_error(tmp_path: Path) -> None:
    """Two answers to "which journal?" is a mistake worth refusing."""
    store = SQLiteStore.open(":memory:")
    try:
        with pytest.raises(TypeError, match="not both"):
            async with satay.run_app(store=store, data_dir=tmp_path):
                pass  # pragma: no cover - the context manager must not open
    finally:
        store.close()


async def test_result_hands_back_parked_when_no_poll_loop_is_running() -> None:
    """Wart 1: a parked run says PARKED, which no workflow can return by accident."""
    store = SQLiteStore.open(":memory:")
    try:
        handle = satay.start(ra_naps, 10, store=store)
        outcome = await handle.result()
        assert outcome is satay.PARKED
        assert outcome is not None
        assert repr(outcome) == "<parked>"
        assert await handle.status() == "waiting"
    finally:
        store.close()


def test_parked_survives_copy_and_pickle_as_the_same_object() -> None:
    """A sentinel you test with ``is`` has to stay one object (KAN-491, ADR-0030).

    Without ``Parked.__reduce__`` both of these hand back a second instance that reprs
    identically and fails every ``is satay.PARKED`` check — the worst kind of bug,
    because the value still *looks* right in a traceback. Anything that snapshots a
    result (``copy.deepcopy`` of a dict of outcomes, a cache that pickles) would have
    quietly broken the contract the whole design rests on.
    """
    assert copy.deepcopy(satay.PARKED) is satay.PARKED
    assert copy.copy(satay.PARKED) is satay.PARKED
    assert pickle.loads(pickle.dumps(satay.PARKED)) is satay.PARKED
    assert copy.deepcopy({"outcome": satay.PARKED})["outcome"] is satay.PARKED


async def test_a_workflow_that_really_returns_none_is_not_parked() -> None:
    """The other half of the ambiguity: ``None`` now means ``None``."""
    store = SQLiteStore.open(":memory:")
    try:
        handle = satay.start(ra_returns_none, 3, store=store)
        assert await handle.result() is None
        assert await handle.status() == "completed"
    finally:
        store.close()


async def test_a_parked_fork_answers_parked_and_completes_under_run_app(tmp_path: Path) -> None:
    """The fork handle applies the identical policy (ADR-0028 + ADR-0030)."""
    data_dir = tmp_path / ".satay"
    async with satay.run_app(data_dir=data_dir, interval=_INTERVAL) as store:
        source = satay.start(ra_naps, 10, store=store)
        assert await source.result() == 21

    # Outside the block there is no poll loop: the fork re-runs the sleep and parks.
    store = SQLiteStore.open(data_dir / "satay.db")
    try:
        parked_fork = await satay.fork(source.run_id, before_task="_ra_double", store=store)
        assert await parked_fork.result() is satay.PARKED
        assert await parked_fork.status() == "waiting"
    finally:
        store.close()

    # Inside one, the same fork call drives all the way through.
    async with satay.run_app(data_dir=data_dir, interval=_INTERVAL) as store:
        driven = await satay.fork(source.run_id, before_task="_ra_double", store=store)
        assert await driven.result() == 21
        assert await driven.status() == "completed"


async def test_result_can_be_bounded_with_wait_for(tmp_path: Path) -> None:
    """A run nobody wakes waits like any other ``await`` — and bounds like one."""
    async with satay.run_app(data_dir=tmp_path / ".satay", interval=_INTERVAL) as store:
        handle = satay.start(ra_waits, 0, store=store)  # nothing ever sends the event
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(handle.result(), timeout=0.1)
