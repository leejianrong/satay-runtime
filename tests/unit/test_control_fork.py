"""Unit tests: fork request validation and journal seeding (N15, V7).

Validation checks the source run, its terminal status (the MVP forks only terminal
runs, ADR-0004/Q53), and the fork-point event; :func:`create_fork` seeds a new run's
journal from the source prefix and records ``RunForked`` lineage, leaving the source
byte-for-byte unchanged.
"""

from __future__ import annotations

import pytest

from satay.api.decorators import task, workflow
from satay.api.primitives import start
from satay.control.commands import ForkValidationError, create_fork, validate_fork_request
from satay.journal.events import Event, EventType, RunRecord, RunStatus, utc_now
from satay.journal.store import SQLiteStore


@task()
async def fk_task(value: int) -> int:
    return value + 1


@workflow
async def fk_wf(value: int) -> int:
    return await fk_task(value)


async def test_valid_fork_request_passes() -> None:
    store = SQLiteStore.open(":memory:")
    await start(fk_wf, 1, store=store, run_id="src").result()
    events = await store.read_events("src")
    # A real event seq on the (terminal) source run is a valid fork-point.
    await validate_fork_request(store, "src", events[0].seq)  # does not raise
    store.close()


async def test_unknown_source_run_is_rejected() -> None:
    store = SQLiteStore.open(":memory:")
    with pytest.raises(ForkValidationError):
        await validate_fork_request(store, "nope", 1)
    store.close()


async def test_fork_point_not_on_run_is_rejected() -> None:
    store = SQLiteStore.open(":memory:")
    await start(fk_wf, 1, store=store, run_id="src").result()
    with pytest.raises(ForkValidationError):
        await validate_fork_request(store, "src", 9999)
    store.close()


async def test_fork_of_non_terminal_run_is_rejected_naming_status() -> None:
    """The MVP forks only terminal runs; a live run is rejected naming its status (Q53)."""
    store = SQLiteStore.open(":memory:")
    await store.create_run(
        RunRecord(
            run_id="live",
            workflow_name="fk_wf",
            status=RunStatus.RUNNING,
            code_version="dev:x",
            created_at=utc_now(),
        )
    )
    await store.append(
        Event(run_id="live", type=EventType.WORKFLOW_CREATED, payload={"workflow_name": "fk_wf"})
    )
    with pytest.raises(ForkValidationError) as excinfo:
        await validate_fork_request(store, "live", 1)
    assert "running" in str(excinfo.value)  # the error names the offending status
    store.close()


async def test_create_fork_seeds_prefix_verbatim_and_records_lineage() -> None:
    """RunForked records the source run and the fork-point; the prefix is copied verbatim."""
    store = SQLiteStore.open(":memory:")
    await start(fk_wf, 1, store=store, run_id="src").result()
    src_events = list(await store.read_events("src"))
    fork_point = max(
        e.seq for e in src_events if e.type is EventType.TASK_COMPLETED
    )  # keep through the task's completion

    workflow_name = await create_fork(
        store,
        source_run_id="src",
        fork_point_seq=fork_point,
        new_run_id="fk",
        now=utc_now(),
        code_version="dev:new",
    )
    assert workflow_name == "fk_wf"

    fk_events = list(await store.read_events("fk"))
    prefix = [e for e in src_events if e.seq <= fork_point]
    # Prefix copied verbatim (type + payload), with fresh event_ids and re-allocated seq.
    assert [e.type for e in fk_events[: len(prefix)]] == [e.type for e in prefix]
    assert [dict(e.payload) for e in fk_events[: len(prefix)]] == [dict(e.payload) for e in prefix]
    assert all(
        f.event_id != s.event_id for f, s in zip(fk_events[: len(prefix)], prefix, strict=True)
    )
    assert [e.seq for e in fk_events[: len(prefix)]] == list(range(1, len(prefix) + 1))

    # The run's own RunForked records exactly the source and the fork-point.
    forked = [e for e in fk_events if e.type is EventType.RUN_FORKED]
    assert len(forked) == 1
    assert forked[0].payload == {"source_run_id": "src", "fork_point_seq": fork_point}

    # The fork's run record carries the NEW (current) code version, not the source's.
    fork_record = await store.get_run("fk")
    assert fork_record is not None and fork_record.code_version == "dev:new"

    # The source run's journal is byte-for-byte unchanged (frozen-dataclass equality).
    assert list(await store.read_events("src")) == src_events
    store.close()
