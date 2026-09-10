"""Projection of a local journal onto the ingest wire, and back (ADR-0044/0045).

The tier-1 journal-ingest contract (ADR-0044) is *the local read format serialised for
the wire*: a shipment is ``{contract_version, producer, tenant, redaction, run, events[]}``
where the plane treats event ``type`` as an opaque string and ``payload`` as an opaque
object, and blobs travel out of band by content id. This module is the pure, stdlib-only
half of that contract — the transform in both directions — with **no network and no new
dependency**. The producer shipper (the ``satay[ingest]`` extra) and the plane (a separate
repo) build on it; nothing in the core imports it back, so deleting this file leaves the
runtime unchanged and releasable (ADR-0045 decision 1, the ADR-0032 test).

It is deliberately **not** re-exported from :mod:`satay` — the top-level surface says which
things are the runtime, and a wire projection is reached by the shipper, not by an author.

Payloads pass through untouched. A caller obtains events via
:meth:`Store.read_raw_events <satay.journal.Store.read_raw_events>`, whose ``payload`` is
``json.loads`` of the stored form with any ``{"$satay": "blobref", …}`` reference left in
place — so a shipment round-trips byte-for-byte and the bytes behind a reference ship
separately (ADR-0044 decision 7). Redaction is **not** performed here: it is the producer's
precondition, upheld before a journal is ever read for shipping (ADR-0029, ADR-0045
decision 7). This module only labels what the store already holds.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from satay.journal.events import EventType, RawEvent, RunRecord, RunStatus

#: The envelope version stamped on every shipment (ADR-0044 decision 3). Semver, and
#: deliberately distinct from a producer's ``code_version`` (opaque run metadata) and the
#: store's ``PRAGMA user_version`` (a local-schema concern that never crosses the wire).
#: Additive fields are a minor bump old planes ignore; a meaning change or a new required
#: field is a major bump.
SHIPMENT_CONTRACT_VERSION = "1.0.0"

#: The major of :data:`SHIPMENT_CONTRACT_VERSION`, the axis the plane negotiates on.
SHIPMENT_CONTRACT_MAJOR = 1

#: Whether the shipment asserts write-time redaction ran (ADR-0029/0044 decision 4). The
#: plane records the assertion against the tenant; it never scans to verify (it cannot read
#: an opaque payload). A custodial tier requires ``"on"``.
RedactionMode = Literal["on", "off"]


@dataclass(frozen=True, slots=True)
class Lineage:
    """A forked run's origin: the run it branched from and the last source ``seq`` copied."""

    source_run_id: str
    fork_point_seq: int


@dataclass(frozen=True, slots=True)
class ParsedShipment:
    """A shipment read back off the wire, e.g. a copy served from the plane (ADR-0044).

    The inverse of :func:`project_run`. ``run`` is reconstructed as a
    :class:`~satay.journal.events.RunRecord` with ``idempotency_key=None`` — the key is
    local-start dedup metadata and, by design, never crosses the wire.
    """

    contract_version: str
    producer: str
    tenant: str
    redaction: RedactionMode
    run: RunRecord
    events: tuple[RawEvent, ...]
    lineage: Lineage | None = None


def lineage_of(events: Sequence[RawEvent]) -> Lineage | None:
    """The run's own fork lineage, or ``None`` if it was not forked.

    A forked run's journal copies its source's prefix, so a fork-of-a-fork carries an
    ancestor's ``RunForked`` too; the run's *own* record is the one with the greatest
    ``seq`` (ADR-0004, mirrored from ``satay.control.views``). Read structurally, without
    this module reaching up into the control package.
    """
    forked = [e for e in events if e.type == EventType.RUN_FORKED.value]
    if not forked:
        return None
    own = max(forked, key=lambda e: e.seq)
    source = own.payload.get("source_run_id")
    seq = own.payload.get("fork_point_seq")
    if not isinstance(source, str) or not isinstance(seq, int):  # pragma: no cover - defensive
        return None
    return Lineage(source_run_id=source, fork_point_seq=seq)


def _event_to_wire(event: RawEvent) -> dict[str, Any]:
    return {
        "seq": event.seq,
        "event_id": event.event_id,
        "type": event.type,
        "ts": event.ts.isoformat(),
        "payload": event.payload,
    }


def project_run(
    run: RunRecord,
    events: Sequence[RawEvent],
    *,
    producer: str,
    tenant: str,
    redaction: RedactionMode,
) -> dict[str, Any]:
    """Project one run and its raw events onto an ingest shipment (ADR-0044 decision 2).

    The result is JSON-serialisable and self-describing: ``contract_version``,
    ``producer``, ``tenant``, ``redaction``, the ``run`` header (with an optional
    ``lineage`` for a forked run), and ``events`` in ``seq`` order. Event ``type`` and
    ``payload`` are carried through opaquely — this is the producer labelling what the
    store holds, not the plane interpreting it. ``events`` should be the run's raw events
    (:meth:`Store.read_raw_events <satay.journal.Store.read_raw_events>`) so blob
    references survive for the out-of-band blob channel.
    """
    header: dict[str, Any] = {
        "run_id": run.run_id,
        "workflow_name": run.workflow_name,
        "status": run.status.value,
        "code_version": run.code_version,
        "created_at": run.created_at.isoformat(),
    }
    lineage = lineage_of(events)
    if lineage is not None:
        header["lineage"] = {
            "source_run_id": lineage.source_run_id,
            "fork_point_seq": lineage.fork_point_seq,
        }
    return {
        "contract_version": SHIPMENT_CONTRACT_VERSION,
        "producer": producer,
        "tenant": tenant,
        "redaction": redaction,
        "run": header,
        "events": [_event_to_wire(e) for e in events],
    }


class ShipmentFormatError(ValueError):
    """Raised when a shipment is structurally malformed (a producer bug, ADR-0044)."""


def _require(mapping: Mapping[str, Any], key: str, kind: type | tuple[type, ...]) -> Any:
    if key not in mapping:
        raise ShipmentFormatError(f"shipment missing required key {key!r}")
    value = mapping[key]
    if not isinstance(value, kind):
        raise ShipmentFormatError(f"shipment key {key!r} has wrong type {type(value).__name__}")
    return value


def _wire_to_event(run_id: str, raw: Mapping[str, Any]) -> RawEvent:
    return RawEvent(
        run_id=run_id,
        seq=_require(raw, "seq", int),
        event_id=_require(raw, "event_id", str),
        type=_require(raw, "type", str),
        ts=datetime.fromisoformat(_require(raw, "ts", str)),
        payload=_require(raw, "payload", Mapping),
    )


def parse_shipment(shipment: Mapping[str, Any]) -> ParsedShipment:
    """Read a shipment back into a :class:`ParsedShipment` (the inverse of :func:`project_run`).

    For a copy served from the plane, where the schema is known again (ADR-0044 decision 2:
    reads run on the producer or against a served copy, never inside the plane). Structural
    only — it does not decode payloads or resolve blobs, so it round-trips exactly what
    :func:`project_run` emitted. Raises :class:`ShipmentFormatError` on a malformed shipment;
    it does not enforce contract-version compatibility, which is the plane's negotiation
    (ADR-0044 decision 3).
    """
    header = _require(shipment, "run", Mapping)
    run_id = _require(header, "run_id", str)
    run = RunRecord(
        run_id=run_id,
        workflow_name=_require(header, "workflow_name", str),
        status=RunStatus(_require(header, "status", str)),
        code_version=_require(header, "code_version", str),
        created_at=datetime.fromisoformat(_require(header, "created_at", str)),
        idempotency_key=None,
    )
    lineage: Lineage | None = None
    if "lineage" in header:
        raw_lineage = _require(header, "lineage", Mapping)
        lineage = Lineage(
            source_run_id=_require(raw_lineage, "source_run_id", str),
            fork_point_seq=_require(raw_lineage, "fork_point_seq", int),
        )
    # ``list``, not ``Sequence``: a JSON array decodes to a list, and a bare ``str`` is a
    # ``Sequence`` too — accepting it would silently iterate an events string per character.
    events = tuple(_wire_to_event(run_id, raw) for raw in _require(shipment, "events", list))
    redaction = _require(shipment, "redaction", str)
    if redaction not in ("on", "off"):
        raise ShipmentFormatError(f"shipment redaction must be 'on' or 'off', got {redaction!r}")
    return ParsedShipment(
        contract_version=_require(shipment, "contract_version", str),
        producer=_require(shipment, "producer", str),
        tenant=_require(shipment, "tenant", str),
        redaction=redaction,
        run=run,
        events=events,
        lineage=lineage,
    )
