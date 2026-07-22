"""Integration test: the run handle drives to terminal and raises a recorded failure."""

from __future__ import annotations

import pytest

from satay.api.decorators import task, workflow
from satay.api.primitives import start
from satay.api.run_handle import WorkflowFailedError
from satay.journal.events import EventType
from satay.journal.store import SQLiteStore


@task()
async def rh_boom(value: int) -> int:
    raise ValueError("kaboom")


@workflow
async def rh_failing(value: int) -> int:
    return await rh_boom(value)


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
