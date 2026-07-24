"""Integration tests for V4 composite primitives (boundary-only, ADR-0011 H3).

The engine reconciles a run driven through ``map`` / ``gather`` against the store
directly — the E2E twins (partial recovery, positional gather, child linkage,
concurrency bound) live in ``tests/e2e/test_composite.py``. These cover the two
boundary behaviours the E2E cases cannot isolate: input-order rejoin under out-of-order
completion, and a nested map resolving item identity by key independent of the ordinal.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from satay.api.decorators import WORKFLOW_ATTR, task, workflow
from satay.api.primitives import gather
from satay.api.primitives import map as satay_map
from satay.api.registry import WorkflowDefinition
from satay.journal.codec import encode
from satay.journal.events import Event, EventType, RunRecord, RunStatus
from satay.journal.store import SQLiteStore
from satay.replay.engine import ReplayEngine

#: Order in which mapped item bodies actually finished (proves out-of-order completion).
COMPLETION_ORDER: list[str] = []


@pytest.fixture(autouse=True)
def _reset() -> None:
    COMPLETION_ORDER.clear()


@task()
async def ooo_item(value: int) -> int:
    """Finish sooner for a larger value (fewer yields), so completion order is reversed."""
    for _ in range(5 - value):
        await asyncio.sleep(0)
    COMPLETION_ORDER.append(f"item-{value}")
    return value * value


@workflow
async def ooo_map_wf(values: list[int]) -> list[int]:
    return await satay_map(ooo_item, values, key=lambda v: f"item-{v}", concurrency=5)


@task()
async def nested_sq(value: int) -> int:
    return value * value


@task()
async def after_task(value: int) -> int:
    return value + 1000


@workflow
async def nested_map_wf(value: int) -> list[object]:
    return await gather(
        satay_map(nested_sq, [1, 2, 3], key=lambda v: f"n{v}", concurrency=3),
        after_task(value),
    )


def _wf_def(fn: object) -> WorkflowDefinition:
    definition: WorkflowDefinition = getattr(fn, WORKFLOW_ATTR)
    return definition


async def _new_run(store: SQLiteStore, run_id: str, workflow_name: str, value: object) -> None:
    await store.create_run(
        RunRecord(
            run_id=run_id,
            workflow_name=workflow_name,
            status=RunStatus.RUNNING,
            code_version="dev:test",
            created_at=datetime(2026, 7, 25, tzinfo=UTC),
        )
    )
    await store.append(
        Event(
            run_id=run_id,
            type=EventType.WORKFLOW_CREATED,
            payload={"workflow_name": workflow_name, "input_ref": encode(value)},
            ts=datetime(2026, 7, 25, tzinfo=UTC),
        )
    )


async def test_map_rejoins_in_input_order_when_items_complete_out_of_order() -> None:
    store = SQLiteStore.open(":memory:")
    values = [1, 2, 3, 4]
    await _new_run(store, "ooo", "ooo_map_wf", values)

    await ReplayEngine(store=store, run_id="ooo").drive(_wf_def(ooo_map_wf), values)

    events = list(await store.read_events("ooo"))
    completed = next(e for e in events if e.type is EventType.WORKFLOW_COMPLETED)
    # Rejoined strictly in INPUT order...
    assert completed.payload["output_ref"] == [1, 4, 9, 16]
    # ...even though the items actually finished in the REVERSE order.
    assert COMPLETION_ORDER == ["item-4", "item-3", "item-2", "item-1"]
    store.close()


async def test_nested_map_resolves_item_identity_by_key_independent_of_ordinal() -> None:
    store = SQLiteStore.open(":memory:")
    await _new_run(store, "nested", "nested_map_wf", 5)

    await ReplayEngine(store=store, run_id="nested").drive(_wf_def(nested_map_wf), 5)

    events = list(await store.read_events("nested"))
    scheduled = [e for e in events if e.type is EventType.TASK_SCHEDULED]

    # The three map items are identified by their nested key= (no ordinal).
    map_items = [e for e in scheduled if e.payload["task_name"] == "nested_sq"]
    assert sorted(e.payload["key"] for e in map_items) == ["n1", "n2", "n3"]
    assert all("ordinal" not in e.payload for e in map_items)

    # The ordinary gather member keeps ordinal 0 — the map's three items never consumed
    # an ordinal, so the ordinal counter is untouched by the fan-out.
    after = next(e for e in scheduled if e.payload["task_name"] == "after_task")
    assert after.payload["ordinal"] == 0
    assert "key" not in after.payload

    completed = next(e for e in events if e.type is EventType.WORKFLOW_COMPLETED)
    assert completed.payload["output_ref"] == [[1, 4, 9], 1005]
    store.close()
