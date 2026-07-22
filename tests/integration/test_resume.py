"""Integration tests for the engine's at-least-once reconciliation (N4/A4.4).

Boundary-only (ADR-0011 H3): a journal is seeded directly into the store, then the
replay engine reconciles it — proving the ambiguous-completion rule (an in-flight
attempt with no ``TaskCompleted`` re-runs; a recorded completion is reused) without the
full ``satay.start`` control flow (that is exercised end-to-end).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from satay.api.decorators import WORKFLOW_ATTR, task, workflow
from satay.api.registry import WorkflowDefinition
from satay.journal.codec import encode
from satay.journal.events import Event, EventType, RunRecord, RunStatus
from satay.journal.store import SQLiteStore
from satay.replay.engine import ReplayEngine

_EXEC: dict[str, int] = {}


@task()
async def rs_step(value: int) -> int:
    _EXEC["rs_step"] = _EXEC.get("rs_step", 0) + 1
    return value + 1


@workflow
async def rs_wf(value: int) -> int:
    return await rs_step(value)


def _wf_def() -> WorkflowDefinition:
    definition: WorkflowDefinition = getattr(rs_wf, WORKFLOW_ATTR)
    return definition


def _ts() -> datetime:
    return datetime(2026, 7, 22, tzinfo=UTC)


async def _seed_run(store: SQLiteStore, run_id: str, *, completed: bool) -> None:
    await store.create_run(
        RunRecord(
            run_id=run_id,
            workflow_name="rs_wf",
            status=RunStatus.RUNNING,
            code_version="dev:test",
            created_at=_ts(),
        )
    )
    await store.append(Event(run_id=run_id, type=EventType.WORKFLOW_CREATED, ts=_ts()))
    await store.append(
        Event(
            run_id=run_id,
            type=EventType.TASK_SCHEDULED,
            payload={"task_name": "rs_step", "ordinal": 0, "input_ref": encode([5])},
            ts=_ts(),
        )
    )
    await store.append(
        Event(
            run_id=run_id,
            type=EventType.TASK_ATTEMPT_STARTED,
            payload={"task_name": "rs_step", "ordinal": 0, "attempt": 1},
            ts=_ts(),
        )
    )
    if completed:
        await store.append(
            Event(
                run_id=run_id,
                type=EventType.TASK_COMPLETED,
                payload={"task_name": "rs_step", "ordinal": 0, "output_ref": encode(99)},
                ts=_ts(),
            )
        )


@pytest.fixture(autouse=True)
def _reset() -> None:
    _EXEC.clear()


async def test_ambiguous_partial_record_is_a_miss_and_reruns() -> None:
    store = SQLiteStore.open(":memory:")
    await _seed_run(store, "amb", completed=False)  # started, never completed

    await ReplayEngine(store=store, run_id="amb").drive(_wf_def(), 5)

    assert _EXEC["rs_step"] == 1  # ambiguous → re-executed (at-least-once)
    events = await store.read_events("amb")
    assert events[-1].type is EventType.WORKFLOW_COMPLETED
    store.close()


async def test_clean_completion_is_a_hit_and_is_reused() -> None:
    store = SQLiteStore.open(":memory:")
    await _seed_run(store, "clean", completed=True)  # recorded TaskCompleted

    await ReplayEngine(store=store, run_id="clean").drive(_wf_def(), 5)

    assert _EXEC.get("rs_step", 0) == 0  # once-recorded → NOT re-executed
    events = await store.read_events("clean")
    completed = next(e for e in events if e.type is EventType.WORKFLOW_COMPLETED)
    assert completed.payload["output_ref"] == 99  # the recorded result was reused
    store.close()
