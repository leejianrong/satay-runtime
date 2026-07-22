"""Integration test: model-usage self-report persists and is retrievable (N14, ADR-0008)."""

from __future__ import annotations

import pytest

from satay import demo
from satay.api.primitives import start
from satay.journal.store import SQLiteStore
from satay.journal.timeline import model_usage


@pytest.fixture(autouse=True)
def _reset() -> None:
    demo.reset_executions()


async def test_record_model_usage_persists_and_the_read_path_retrieves_it() -> None:
    store = SQLiteStore.open(":memory:")
    await start(demo.usage_demo, 1, store=store).result()

    events = await store.read_events((await store.list_runs())[0])
    usage = model_usage(events)
    assert usage == [{"model": "demo-model", "input_tokens": 10, "output_tokens": 5}]
    store.close()


async def test_non_reporting_task_records_no_usage() -> None:
    store = SQLiteStore.open(":memory:")
    await start(demo.quiet_demo, 1, store=store).result()

    events = await store.read_events((await store.list_runs())[0])
    assert model_usage(events) == []
    store.close()
