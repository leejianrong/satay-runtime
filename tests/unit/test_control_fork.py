"""Unit tests: fork request validation checks source run + fork-point (N15, V5 stub)."""

from __future__ import annotations

import pytest

from satay.api.decorators import task, workflow
from satay.api.primitives import start
from satay.control.commands import ForkValidationError, validate_fork_request
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
    # A real event seq on the source run is a valid fork-point.
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
