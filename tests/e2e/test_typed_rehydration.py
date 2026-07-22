"""E2E: typed rehydration through the seam (annotated → type, unannotated → dict)."""

from __future__ import annotations

import dataclasses

from satay.api.decorators import task, workflow
from satay.api.primitives import start
from satay.journal.store import SQLiteStore


@dataclasses.dataclass
class Receipt:
    total: int
    currency: str


@task()
async def tr_make_receipt(amount: int) -> Receipt:
    return Receipt(total=amount, currency="SGD")


@workflow
async def tr_annotated(amount: int) -> Receipt:
    return await tr_make_receipt(amount)


@task()
async def tr_make_dict(amount: int):  # type: ignore[no-untyped-def]  # intentionally unannotated
    return {"total": amount, "currency": "SGD"}


@workflow
async def tr_unannotated(amount: int):  # type: ignore[no-untyped-def]
    return await tr_make_dict(amount)


async def test_annotated_result_returns_declared_type() -> None:
    store = SQLiteStore.open(":memory:")
    result = await start(tr_annotated, 42, store=store).result()
    assert isinstance(result, Receipt)
    assert result.total == 42
    assert result.currency == "SGD"
    store.close()


async def test_unannotated_result_returns_dict() -> None:
    store = SQLiteStore.open(":memory:")
    result = await start(tr_unannotated, 42, store=store).result()
    assert isinstance(result, dict)
    assert result == {"total": 42, "currency": "SGD"}
    store.close()
