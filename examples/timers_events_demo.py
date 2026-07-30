"""Durable waiting: ``satay.sleep``, ``wait_for_event`` / ``send_event``, and the timeout.

Three runs, one data dir:

1. **sleep** — a workflow sleeps 8 hours between two tasks. It does not hold a coroutine
   open for 8 hours: it records a timer, **parks** (status ``waiting``, released from
   memory), and the worker re-drives it when the timer comes due.
2. **event** — a workflow blocks on ``wait_for_event(ShipmentArrived, key=...)`` and
   resumes when someone calls ``satay.send_event`` with a matching key.
3. **timeout** — the same wait with ``timeout=``. Nobody sends anything, the timeout
   fires, the wait resolves to ``None``, and the workflow takes its escalation branch.

    uv run python examples/timers_events_demo.py        # throwaway temp data dir
    SATAY_DATA_DIR=.satay-demo uv run python examples/timers_events_demo.py

Nothing here waits on real time. ``satay.testing.ManualClock`` *is* the clock the timer
loop reads, so "8 hours later" is one ``clock.advance(...)`` call, and a ``tick()`` of
``TimerEventWorker`` is one pass of the poll loop that ``satay dev`` otherwise runs for
you in the background. Write your own timer tests exactly this way.

By default the runs land in a throwaway temp directory, so this file is self-contained
wherever you download it. Set ``SATAY_DATA_DIR`` (or pass a path as the first argument)
to keep the journal, then ``satay dev --data-dir <that path>`` opens it in Studio.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import satay
from satay.config import DATA_DIR_ENV_VAR, db_path
from satay.journal.store import SQLiteStore
from satay.journal.timeline import render_timeline
from satay.testing import ManualClock
from satay.timers import TimerEventWorker

#: Physical executions per task name, so "reused after the wake" is observable.
EXECUTIONS: dict[str, int] = {}

_HOUR = timedelta(hours=1)


def record(name: str) -> None:
    EXECUTIONS[name] = EXECUTIONS.get(name, 0) + 1


@dataclass(frozen=True)
class ShipmentArrived:
    """An external event delivered with ``satay.send_event``.

    A frozen dataclass is enough — the codec tags and round-trips dataclasses, enums,
    datetimes and timedeltas, and ``wait_for_event(ShipmentArrived, ...)`` rehydrates the
    delivered payload back into this class (there is no pickle anywhere, ADR-0005).
    """

    carrier: str
    crates: int


# -- 1: durable sleep ------------------------------------------------------------


@satay.task()
async def count_stock(sku: str) -> int:
    record("count_stock")
    return 12


@satay.task()
async def reorder(sku: str, on_hand: int) -> str:
    record("reorder")
    return f"ordered {40 - on_hand} units of {sku}"


@satay.workflow
async def overnight_restock(sku: str) -> str:
    """Count stock, wait out the 8-hour settlement window, then reorder."""
    on_hand = await count_stock(sku)
    await satay.sleep(8 * _HOUR)  # durable: recorded as a timer, the run parks here
    return await reorder(sku, on_hand)


# -- 2 and 3: waiting for an external event --------------------------------------


@satay.workflow
async def await_shipment(order_id: str) -> str:
    """Block until a ``ShipmentArrived`` event lands on ``order_id``."""
    arrival = await satay.wait_for_event(ShipmentArrived, key=order_id)
    return f"{arrival.crates} crates received via {arrival.carrier}"


@satay.workflow
async def await_shipment_or_escalate(order_id: str) -> str:
    """The same wait, bounded. A timed-out wait resolves to ``None`` — not an error."""
    arrival = await satay.wait_for_event(ShipmentArrived, key=order_id, timeout=6 * _HOUR)
    if arrival is None:
        return f"escalated: nothing arrived for {order_id} within 6h"
    return f"{arrival.crates} crates received via {arrival.carrier}"


def resolve_workdir() -> tuple[Path, bool]:
    """Where these runs' journals live, and whether they outlive the process.

    An explicit argument or ``SATAY_DATA_DIR`` means the caller wants the journal kept
    (so Studio can open it); with neither, fall back to a throwaway temp directory so the
    file stays self-contained wherever it is downloaded and run.
    """
    override = sys.argv[1] if len(sys.argv) > 1 else os.environ.get(DATA_DIR_ENV_VAR)
    if override:
        workdir = Path(override).expanduser().resolve()
        workdir.mkdir(parents=True, exist_ok=True)
        return workdir, True
    return Path(tempfile.mkdtemp(prefix="satay-timers-")), False


async def show(store: SQLiteStore, run_id: str) -> None:
    print(render_timeline(list(await store.read_events(run_id)), run_id=run_id))
    print()


async def main() -> None:
    workdir, durable = resolve_workdir()
    store = SQLiteStore.open(db_path(workdir))
    clock = ManualClock()
    # The poll loop `satay dev` runs in the background. Here we drive it one tick at a
    # time, off the same manual clock the runs use, so wakes are deterministic.
    worker = TimerEventWorker(store=store, clock=clock)

    print("Satay — durable sleep, events, and the timeout path")
    print(f"data dir: {workdir}\n")

    # -- 1: sleep ---------------------------------------------------------------------
    restock = satay.start(overnight_restock, "SKU-42", store=store, clock=clock)
    print(f"1) overnight_restock sleeps 8h — run {restock.run_id}")
    first_drive = await restock.result()
    print(f"  first drive returned {first_drive} — status {await restock.status()} (parked)")
    print(f"  count_stock executions: {EXECUTIONS['count_stock']}  reorder: not yet reached")
    print(f"  worker tick with nothing due: {await worker.tick()} run(s) woken")

    clock.advance(8 * 3600)  # 8 hours later, as far as the runtime is concerned
    print(f"  after advancing 8h, worker tick: {await worker.tick()} run(s) woken")
    print(f"  result: {await restock.result()}   status: {await restock.status()}")
    print(
        f"  count_stock executions: {EXECUTIONS['count_stock']} (reused on the wake, "
        f"not re-run)  reorder: {EXECUTIONS['reorder']}"
    )
    print("  note: a graceful wake from a park is not an interruption — no ⚡ below\n")
    await show(store, restock.run_id)

    # -- 2: event delivery ------------------------------------------------------------
    shipment = satay.start(await_shipment, "order-7", store=store, clock=clock)
    print(f"2) await_shipment blocks on an event — run {shipment.run_id}")
    print(f"  first drive returned {await shipment.result()} — status {await shipment.status()}")
    await satay.send_event(ShipmentArrived(carrier="DHL", crates=4), key="order-7", store=store)
    print("  sent ShipmentArrived(key='order-7') — it sits in the durable inbox")
    print(f"  worker tick: {await worker.tick()} run(s) woken")
    print(f"  result: {await shipment.result()}   status: {await shipment.status()}")
    print("  (an event that arrives *before* the wait is matched from the inbox too)\n")
    await show(store, shipment.run_id)

    # -- 3: the timeout path ----------------------------------------------------------
    escalate = satay.start(await_shipment_or_escalate, "order-8", store=store, clock=clock)
    print(f"3) await_shipment_or_escalate times out — run {escalate.run_id}")
    print(f"  first drive returned {await escalate.result()} — status {await escalate.status()}")
    clock.advance(6 * 3600)  # past the 6h timeout, with nobody sending an event
    print(f"  after advancing 6h, worker tick: {await worker.tick()} run(s) woken")
    print(f"  result: {await escalate.result()}   status: {await escalate.status()}")
    print("  the wait resolved to None and the workflow chose its own escalation branch\n")
    await show(store, escalate.run_id)

    store.close()

    if durable:
        print(f"journal kept in {workdir}")
        print(f"all three runs are on the run list:  satay dev --data-dir {workdir}")
    else:
        print(
            f"journals went to a temp dir ({workdir}) and are not worth keeping.\n"
            f"Re-run with SATAY_DATA_DIR set to browse them in Studio."
        )


if __name__ == "__main__":
    asyncio.run(main())
