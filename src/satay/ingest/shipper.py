"""The producer shipper: ship a local run's journal to a tier-1 ingest plane (ADR-0045).

This is the ``satay[ingest]`` extra's control flow — pure over a :class:`Transport` seam, so
it carries **no third-party dependency of its own** (the HTTP transport that does is
:mod:`satay.ingest.http`, imported only when you want it). It is in-tree so it can read raw
events through the store, but it is not re-exported from :mod:`satay` and nothing in the
core imports it: delete the ``satay/ingest/`` package and the runtime is unchanged
(ADR-0045 decision 1, the ADR-0032 test).

What it does, per ADR-0044/0045:

- **Redaction precondition (decision 7).** A custodial endpoint requires write-time
  redaction (ADR-0029). The shipper reads the store's mode and *refuses to ship* an
  unredacted journal to such an endpoint, rather than sending cleartext value slots. The
  plane records the assertion; it never scans.
- **Blob channel (decision 7).** A spilled value is a content-addressed reference in the
  payload; its bytes travel out of band. The shipper uploads a referenced blob the plane
  lacks (``blob_present`` then ``put_blob``) *before* the event that names it, and dedupes
  within a shipment so a blob shared across a run and its forks uploads once.
- **Incremental, at-least-once delivery (decision 6).** It ships only events past the
  plane's acknowledged ``seq`` (so a parked run advances and a crashed producer resumes),
  and re-shipping is safe because the plane keys on ``(tenant, run_id, seq)``.
- **Backpressure (ADR-0045 decision 6).** A ``429`` surfaces as :class:`BackpressureError`;
  the shipper honours ``Retry-After`` or falls back to the runtime's own bounded backoff
  (:func:`satay.executor.backoff_delay`) — no second retry policy.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from satay.blobs import BlobStore, is_blob_ref
from satay.executor import backoff_delay
from satay.journal.events import RawEvent, RunRecord
from satay.journal.wire import RedactionMode, project_run
from satay.testing.rng import Rng, SystemRng

#: Default cap on how many times one operation is retried through backpressure before the
#: shipper gives up and re-raises the :class:`BackpressureError`. A cooperative producer
#: over quota should slow down, but it should not spin forever.
DEFAULT_MAX_BACKPRESSURE_RETRIES = 6


class IngestError(Exception):
    """Base for every shipper-raised error."""


class UnknownRunError(IngestError):
    """The run id is not in the store."""


class RedactionRequiredError(IngestError):
    """A custodial endpoint requires write-time redaction, but the store's is off (ADR-0029).

    Raised *before* any byte leaves the process — the shipper refuses to send cleartext
    value slots to a store it does not operate, rather than trusting the plane to scrub.
    """


class MissingBlobError(IngestError):
    """A payload references a blob whose bytes the shipper cannot supply.

    Either no :class:`~satay.blobs.BlobStore` was passed, or the referenced content is not
    in it — in both cases the plane would be left with a reference it can never resolve.
    """


class BackpressureError(IngestError):
    """A transport signals the plane is over quota (``429``); ``retry_after`` is its hint.

    The transport translates the plane's ``429`` into this; the shipper owns the waiting
    (``retry_after`` seconds when given, else the runtime's bounded backoff). Kept an
    exception rather than a return value so a transport cannot silently drop a shipment.
    """

    def __init__(self, message: str = "", *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


@dataclass(frozen=True, slots=True)
class ShipmentAck:
    """The plane's answer to a shipment: the highest **contiguous** ``seq`` it has stored."""

    ack_seq: int


@dataclass(frozen=True, slots=True)
class ShipResult:
    """The outcome of one :func:`ship_run` call."""

    run_id: str
    redaction: RedactionMode
    blobs_uploaded: int
    #: ``seq`` of the first event this call shipped, or ``None`` if there was nothing new.
    first_seq_shipped: int | None
    events_shipped: int
    #: The plane's acknowledged high-water ``seq`` after this call.
    ack_seq: int


class ShippableStore(Protocol):
    """The narrow view of a store the shipper needs — its own, not the core ``Store`` seam.

    Defined here so shipping adds nothing to the core :class:`~satay.journal.Store` protocol
    (ADR-0045 decision 1). :class:`~satay.journal.store.SQLiteStore` satisfies it
    structurally.
    """

    @property
    def write_redaction_enabled(self) -> bool: ...

    async def get_run(self, run_id: str) -> RunRecord | None: ...

    async def read_raw_events(self, run_id: str) -> Sequence[RawEvent]: ...


class Transport(Protocol):
    """The seam between the shipper and a plane (ADR-0044 is transport-agnostic by design).

    :mod:`satay.ingest.http` is the HTTP+NDJSON implementation; a test supplies an in-memory
    fake. A method that hits an over-quota plane raises :class:`BackpressureError`; every other
    plane error is the transport's own exception to raise.
    """

    @property
    def requires_redaction(self) -> bool:
        """Whether this endpoint is custodial and so requires ``redaction="on"``."""
        ...

    async def current_ack(self, tenant: str, run_id: str) -> int:
        """The highest contiguous ``seq`` already stored for this run (``0`` if none)."""
        ...

    async def send(self, shipment: Mapping[str, Any]) -> ShipmentAck:
        """Ingest a shipment (header + tail events); return the new acknowledgement."""
        ...

    async def blob_present(self, blob_id: str) -> bool:
        """Whether the plane already holds this content (the ``HEAD`` of the blob channel)."""
        ...

    async def put_blob(self, blob_id: str, data: bytes) -> None:
        """Upload one blob's bytes (the ``PUT`` of the blob channel)."""
        ...


def _blob_ids_in(value: Any) -> Iterator[str]:
    """Every blob id referenced anywhere inside a payload value.

    Spill is top-level (ADR-0004), but a foreign producer's payload may nest, so this walks
    mappings and sequences. A blob reference is a leaf — its own ``id`` is yielded and it is
    not descended into.
    """
    if is_blob_ref(value):
        yield value["id"]
        return
    if isinstance(value, Mapping):
        for item in value.values():
            yield from _blob_ids_in(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _blob_ids_in(item)


async def _through_backpressure[T](
    op: Callable[[], Awaitable[T]],
    *,
    rng: Rng,
    sleep: Callable[[float], Awaitable[None]],
    max_retries: int,
) -> T:
    """Run ``op``, waiting out :class:`BackpressureError` up to ``max_retries`` times.

    Honours the plane's ``Retry-After`` when given, else the runtime's jittered bounded
    backoff (:func:`satay.executor.backoff_delay`, reused rather than reinvented).
    """
    failures = 0
    while True:
        try:
            return await op()
        except BackpressureError as bp:
            failures += 1
            if failures > max_retries:
                raise
            delay = bp.retry_after if bp.retry_after is not None else backoff_delay(failures, rng)
            await sleep(delay)


async def _ship_blobs(
    events: Sequence[RawEvent],
    blobs: BlobStore | None,
    transport: Transport,
    *,
    rng: Rng,
    sleep: Callable[[float], Awaitable[None]],
    max_retries: int,
) -> int:
    """Upload the blobs these events reference that the plane lacks; return the count.

    Runs before the events are sent, so a referenced blob is present when the event that
    names it lands. Dedupes within the call so a blob shared across events (a fork) uploads
    once.
    """
    uploaded = 0
    seen: set[str] = set()
    for event in events:
        for blob_id in _blob_ids_in(event.payload):
            if blob_id in seen:
                continue
            seen.add(blob_id)
            if await _upload_blob_if_missing(
                transport, blobs, blob_id, rng=rng, sleep=sleep, max_retries=max_retries
            ):
                uploaded += 1
    return uploaded


async def _upload_blob_if_missing(
    transport: Transport,
    blobs: BlobStore | None,
    blob_id: str,
    *,
    rng: Rng,
    sleep: Callable[[float], Awaitable[None]],
    max_retries: int,
) -> bool:
    """Upload one blob if the plane lacks it; return whether it was uploaded.

    ``blob_id`` is a parameter, not a loop variable, so the closures below bind exactly the
    blob under consideration — no default-argument trick, and the generic retry wrapper's
    type still infers.
    """
    present = await _through_backpressure(
        lambda: transport.blob_present(blob_id), rng=rng, sleep=sleep, max_retries=max_retries
    )
    if present:
        return False
    if blobs is None:
        raise MissingBlobError(
            f"payload references blob {blob_id!r} but no BlobStore was provided to ship it"
        )
    try:
        data = blobs.get(blob_id)
    except FileNotFoundError as exc:
        raise MissingBlobError(
            f"payload references blob {blob_id!r} which is not in the local blob store"
        ) from exc
    await _through_backpressure(
        lambda: transport.put_blob(blob_id, data), rng=rng, sleep=sleep, max_retries=max_retries
    )
    return True


async def ship_run(
    run_id: str,
    *,
    store: ShippableStore,
    transport: Transport,
    producer: str,
    tenant: str,
    blobs: BlobStore | None = None,
    rng: Rng | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    max_backpressure_retries: int = DEFAULT_MAX_BACKPRESSURE_RETRIES,
) -> ShipResult:
    """Ship one run's journal to ``transport``, incrementally and at-least-once (ADR-0044/0045).

    Reads the run and its raw events, enforces the redaction precondition, uploads any
    referenced blobs the plane lacks, then sends the events past the plane's acknowledged
    ``seq``. Idempotent: calling it again after a partial ship re-sends only what the plane
    has not acknowledged, and re-sending a stored event is a plane-side no-op.

    ``blobs`` supplies the bytes behind spilled references (pass the run's
    :class:`~satay.blobs.BlobStore`); it may be omitted for a run known not to spill.
    ``rng`` and ``sleep`` are injected for deterministic backpressure tests (ADR-0011);
    the defaults are the real ones.

    Raises :class:`UnknownRunError`, :class:`RedactionRequiredError`, or
    :class:`MissingBlobError`; a persistent over-quota plane surfaces the last
    :class:`BackpressureError` after ``max_backpressure_retries``.
    """
    resolved_rng = rng if rng is not None else SystemRng()
    run = await store.get_run(run_id)
    if run is None:
        raise UnknownRunError(run_id)

    redaction: RedactionMode = "on" if store.write_redaction_enabled else "off"
    if transport.requires_redaction and redaction != "on":
        raise RedactionRequiredError(
            f"endpoint requires write-time redaction, but run {run_id!r}'s store records "
            "verbatim (ADR-0029); enable write_redaction before shipping to a custodial plane"
        )

    events = await store.read_raw_events(run_id)
    ack = await _through_backpressure(
        lambda: transport.current_ack(tenant, run_id),
        rng=resolved_rng,
        sleep=sleep,
        max_retries=max_backpressure_retries,
    )
    tail = [event for event in events if event.seq > ack]

    uploaded = await _ship_blobs(
        tail, blobs, transport, rng=resolved_rng, sleep=sleep, max_retries=max_backpressure_retries
    )

    if not tail:
        # Already up to date: blobs are reconciled above, and there is no new event to send.
        return ShipResult(
            run_id=run_id,
            redaction=redaction,
            blobs_uploaded=uploaded,
            first_seq_shipped=None,
            events_shipped=0,
            ack_seq=ack,
        )

    shipment = project_run(
        run, events, producer=producer, tenant=tenant, redaction=redaction, since_seq=ack
    )
    result = await _through_backpressure(
        lambda: transport.send(shipment),
        rng=resolved_rng,
        sleep=sleep,
        max_retries=max_backpressure_retries,
    )
    return ShipResult(
        run_id=run_id,
        redaction=redaction,
        blobs_uploaded=uploaded,
        first_seq_shipped=tail[0].seq,
        events_shipped=len(tail),
        ack_seq=result.ack_seq,
    )
