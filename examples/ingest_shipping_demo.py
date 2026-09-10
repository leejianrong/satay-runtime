"""Ship a run's journal to a tier-1 ingest plane, incrementally and at-least-once.

The hosted product (ADR-0041/0044/0045) keeps a redacted, replayable copy of your journals
so a team can share run history and gate on it. The producer side of that — turning a local
journal into shipments and sending them — is ``satay.ingest``, the ``satay[ingest]`` extra.
This file is that loop end to end, against an **in-process reference plane** so it runs with
no network, no account, and no HTTP client:

    uv run python examples/ingest_shipping_demo.py        # throwaway temp data dir
    SATAY_DATA_DIR=.satay-demo uv run python examples/ingest_shipping_demo.py

What it demonstrates, in order:

1. **A real run.** A two-step ``assemble_briefing`` workflow whose research step returns a
   payload over the 256 KiB spill threshold, so the journal carries a **content-addressed
   blob reference** rather than the bytes inline (ADR-0004).
2. **The redaction precondition.** A *custodial* endpoint requires write-time redaction
   (ADR-0029); shipping an unredacted store to one is refused **before any byte leaves the
   process** (``RedactionRequiredError``), never silently sent. The demo store records
   verbatim, so this is the refusal you would hit — shown, then sidestepped by shipping to a
   non-custodial endpoint.
3. **The first shipment.** ``ship_run`` projects the run onto the ADR-0044 envelope, uploads
   the referenced blob out of band (``HEAD`` then ``PUT``) *before* the event that names it,
   and sends the events. The plane ends up holding the whole run.
4. **Incremental, at-least-once.** Shipping the same terminal run again sends nothing — the
   plane's acknowledged ``seq`` already covers it — and re-uploads no blob. That idempotence
   is what lets a producer resume after a crash and a parked run ship as it grows.
5. **A served copy round-trips.** What the plane stored parses back through
   ``parse_shipment`` into the same run header and events — the plane never had to understand
   a single Satay event ``type`` to store and serve them (the schema-agnostic contract).

**The plane here is a fake.** ``InMemoryPlane`` implements the one-method-per-verb
``satay.ingest.Transport`` seam in a dict; the real transport is
``satay.ingest.http.HttpTransport`` (HTTP + NDJSON, bearer auth), which needs the
``satay[ingest]`` extra and a plane to talk to. The seam is why this example needs neither.

**Why the work lives in tasks.** A workflow body replays from the top on every resume, so a
large or nondeterministic value produced inline would differ the second time. Producing it
in a ``@satay.task`` records it once — which is also what gives the journal a stable blob to
ship.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from collections.abc import Mapping
from importlib.metadata import version
from pathlib import Path
from typing import Any

import satay
from satay.blobs import BlobStore
from satay.config import BLOB_DIR_NAME, DATA_DIR_ENV_VAR, db_path
from satay.ingest import RedactionRequiredError, ShipmentAck, ship_run
from satay.journal.store import SQLiteStore
from satay.journal.wire import parse_shipment

#: Over the 256 KiB spill threshold once encoded, so the research result spills to a blob.
_BIG = "lorem ipsum dolor sit amet " * 12_000


@satay.task()
async def research(topic: str, /) -> str:
    """A deterministic 'expensive' step whose large result spills to a content-addressed blob."""
    return f"Findings on {topic}:\n{_BIG}"


@satay.task()
async def summarize(document: str, /) -> str:
    """Reduce the large document to a one-line summary (small, stays inline)."""
    return f"{document[: document.index(chr(10))]} ({len(document)} chars reviewed)"


@satay.workflow
async def assemble_briefing(topic: str) -> str:
    document = await research(topic)
    return await summarize(document)


class InMemoryPlane:
    """A stand-in ingest plane implementing :class:`satay.ingest.Transport` in a dict.

    Honours the ADR-0044 invariants a real plane must: it acknowledges the highest
    *contiguous* ``seq`` it holds, dedupes blobs by content id, and stores event ``type`` and
    ``payload`` opaquely — it never interprets them. ``requires_redaction`` mirrors a
    custodial endpoint's policy.
    """

    def __init__(self, *, requires_redaction: bool = False) -> None:
        self.requires_redaction = requires_redaction
        self.meta: dict[str, Any] = {}
        self.header: dict[str, Any] | None = None
        self.events: dict[int, dict[str, Any]] = {}
        self.blobs: dict[str, bytes] = {}

    def _ack(self) -> int:
        ack = 0
        for seq in sorted(self.events):
            if seq != ack + 1:
                break
            ack = seq
        return ack

    async def current_ack(self, tenant: str, run_id: str) -> int:
        return self._ack()

    async def send(self, shipment: Mapping[str, Any]) -> ShipmentAck:
        self.meta = {
            k: shipment[k] for k in ("contract_version", "producer", "tenant", "redaction")
        }
        self.header = dict(shipment["run"])
        for event in shipment["events"]:
            self.events[event["seq"]] = event
        return ShipmentAck(ack_seq=self._ack())

    async def blob_present(self, blob_id: str) -> bool:
        return blob_id in self.blobs

    async def put_blob(self, blob_id: str, data: bytes) -> None:
        self.blobs[blob_id] = data

    def served_copy(self) -> dict[str, Any]:
        """Reassemble the shipment the plane would serve back, for a producer to parse."""
        return {
            **self.meta,
            "run": self.header,
            "events": [self.events[seq] for seq in sorted(self.events)],
        }


def resolve_workdir() -> tuple[Path, bool]:
    """Where this run's journal lives, and whether it outlives the process."""
    override = sys.argv[1] if len(sys.argv) > 1 else os.environ.get(DATA_DIR_ENV_VAR)
    if override:
        workdir = Path(override).expanduser().resolve()
        workdir.mkdir(parents=True, exist_ok=True)
        return workdir, True
    return Path(tempfile.mkdtemp(prefix="satay-ingest-")), False


async def main() -> None:
    workdir, durable = resolve_workdir()
    producer = f"satay/{version('satay')}"
    store = SQLiteStore.open(db_path(workdir))
    blobs = BlobStore(workdir / BLOB_DIR_NAME)

    print("Satay — shipping a journal to a tier-1 ingest plane (the satay[ingest] producer)")
    print(f"data dir: {workdir}")
    print(f"producer: {producer}\n")

    # 1. A real run whose research step spills a blob.
    handle = satay.start(assemble_briefing, "sea otters", store=store)
    summary = await handle.result()
    run_id = handle.run_id
    raw = await store.read_raw_events(run_id)
    redaction = "on" if store.write_redaction_enabled else "off"
    print(f"1. ran assemble_briefing → {summary!r}")
    print(f"   journal: {len(raw)} events, write-time redaction {redaction}\n")

    # 2. A custodial endpoint refuses an unredacted store, before anything is sent.
    try:
        await ship_run(
            run_id,
            store=store,
            transport=InMemoryPlane(requires_redaction=True),
            producer=producer,
            tenant="acme",
            blobs=blobs,
        )
    except RedactionRequiredError:
        print("2. a custodial endpoint refused the unredacted journal (as designed) —")
        print("   nothing left the process\n")

    # 3. Ship to a non-custodial endpoint: blob out of band, then the events.
    plane = InMemoryPlane()
    first = await ship_run(
        run_id, store=store, transport=plane, producer=producer, tenant="acme", blobs=blobs
    )
    print(
        f"3. shipped: {first.events_shipped} events (seq 1..{first.ack_seq}), "
        f"{first.blobs_uploaded} blobs uploaded out of band"
    )
    print("   the plane holds the references, not the bytes behind them\n")

    # 4. Re-shipping a terminal run is a no-op — the ack already covers it.
    again = await ship_run(
        run_id, store=store, transport=plane, producer=producer, tenant="acme", blobs=blobs
    )
    print(
        f"4. re-shipped: {again.events_shipped} events, {again.blobs_uploaded} blobs — "
        "already up to date (at-least-once is safe)\n"
    )

    # 5. What the plane stored parses back, without the plane knowing any Satay event type.
    parsed = parse_shipment(plane.served_copy())
    print(
        f"5. served copy parses back: run {parsed.run.run_id[:8]}… is "
        f"{parsed.run.status}, {len(parsed.events)} events"
    )
    print("   'type' and 'payload' were stored opaquely — a non-Satay producer ships the same\n")

    store.close()
    if durable:
        print(f"journal kept in {workdir}")
    else:
        print(f"journal went to a temp dir ({workdir}) and is not worth keeping.")


if __name__ == "__main__":
    asyncio.run(main())
