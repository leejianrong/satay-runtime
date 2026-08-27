"""Integration tests: the run handle drives to terminal, raises a recorded failure, and
answers ``status()`` with a ``RunStatus`` member (KAN-524)."""

from __future__ import annotations

import pytest

from satay.api.decorators import task, workflow
from satay.api.primitives import start
from satay.api.run_handle import WorkflowFailedError
from satay.journal.events import EventType, RunStatus
from satay.journal.store import SQLiteStore


@task()
async def rh_boom(value: int) -> int:
    raise ValueError("kaboom")


@workflow
async def rh_failing(value: int) -> int:
    return await rh_boom(value)


@task()
async def rh_double(value: int) -> int:
    return value * 2


@workflow
async def rh_ok(value: int) -> int:
    return await rh_double(value)


async def test_result_raises_recorded_failure_and_records_workflow_failed() -> None:
    store = SQLiteStore.open(":memory:")
    handle = start(rh_failing, 1, store=store)
    with pytest.raises(WorkflowFailedError) as excinfo:
        await handle.result()
    assert excinfo.value.error_type == "ValueError"
    assert "kaboom" in excinfo.value.error_message
    assert "Traceback" in excinfo.value.traceback_str

    events = await store.read_events(handle.run_id)
    assert events[-1].type is EventType.WORKFLOW_FAILED
    assert await handle.status() == "failed"
    store.close()


async def test_status_answers_with_a_run_status_member_not_a_bare_string() -> None:
    """``status()`` returns the enum, and the old string form still compares equal.

    Both halves matter. The enum is the point of KAN-524 — ``is RunStatus.COMPLETED``
    cannot be misspelled the way ``== "compleeted"`` silently can, and it is what lets
    a ``match`` be checked for exhaustiveness. The string equality is the compatibility
    promise: ``RunStatus`` is a :class:`enum.StrEnum`, so every ``== "completed"`` a user
    (or this suite) already wrote keeps working, and interpolation still renders the bare
    value rather than ``RunStatus.COMPLETED``.
    """
    store = SQLiteStore.open(":memory:")
    handle = start(rh_ok, 1, store=store)
    assert await handle.result() == 2

    status = await handle.status()
    assert isinstance(status, RunStatus)
    assert status is RunStatus.COMPLETED
    assert status == "completed"
    assert f"{status}" == "completed"
    store.close()
