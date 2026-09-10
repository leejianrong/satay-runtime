"""The HTTP+NDJSON transport for the ingest shipper (ADR-0044/0045 decision 2).

The concrete :class:`~satay.ingest.shipper.Transport` a producer uses against a real plane.
It needs an HTTP client, so it lives behind the ``satay[ingest]`` extra and is imported
explicitly — ``import satay.ingest`` (the pure shipper) does **not** pull this in, and the
runtime core never imports either (ADR-0045 decision 1).

The framing is ADR-0045 decision 2: a shipment is sent as gzip-compressed newline-delimited
JSON — the run header on the first line, then one event per line — which a non-Satay producer
can emit with nothing more than ``json.dumps`` and a ``for`` loop. Authentication is a
per-tenant bearer token (decision 3); the plane resolves the tenant from the token, so the
paths carry only ``run_id`` and blob content id. A ``429`` becomes a
:class:`~satay.ingest.shipper.BackpressureError` carrying the ``Retry-After`` hint, which is
the one plane response the shipper is expected to wait out rather than fail on.

The endpoint shape here is the reference the tier-1 plane (a separate repo, ADR-0045 Phase C)
implements against; nothing about it crosses into the core.
"""

from __future__ import annotations

import gzip
import json
import math
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

import httpx

from satay.ingest.shipper import BackpressureError, PlaneResponseError, ShipmentAck

#: Default per-request timeout, in seconds.
DEFAULT_TIMEOUT_SECONDS = 30.0


class HttpTransport:
    """Ship over HTTP+NDJSON to a tier-1 ingest plane, authenticated by a bearer token.

    Pass a ``base_url`` and ``token``; the transport owns an :class:`httpx.AsyncClient` it
    closes on :meth:`aclose` (or ``async with``). Inject ``client`` to reuse a pre-built one
    (a test supplies an :class:`httpx.MockTransport`-backed client) — an injected client is
    the caller's to close. ``requires_redaction`` says whether this endpoint is custodial;
    the shipper reads it to enforce the ADR-0029 precondition before sending.
    """

    def __init__(
        self,
        base_url: str,
        *,
        token: str,
        requires_redaction: bool = True,
        client: httpx.AsyncClient | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._requires_redaction = requires_redaction
        self._owns_client = client is None
        self._client = client if client is not None else httpx.AsyncClient(timeout=timeout)

    @property
    def requires_redaction(self) -> bool:
        return self._requires_redaction

    def _headers(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self._token}"}
        if extra:
            headers.update(extra)
        return headers

    async def current_ack(self, tenant: str, run_id: str) -> int:
        response = await self._client.get(
            f"{self._base_url}/v1/runs/{quote(run_id, safe='')}/ack",
            params={"tenant": tenant},
            headers=self._headers(),
        )
        if response.status_code == httpx.codes.NOT_FOUND:
            return 0  # the plane has never seen this run
        _raise_for_backpressure(response)
        response.raise_for_status()
        return _read_ack_seq(response)

    async def send(self, shipment: Mapping[str, Any]) -> ShipmentAck:
        run_id = shipment["run"]["run_id"]
        body = gzip.compress(_encode_ndjson(shipment))
        response = await self._client.post(
            f"{self._base_url}/v1/runs/{quote(run_id, safe='')}/events",
            content=body,
            headers=self._headers(
                {"Content-Type": "application/x-ndjson", "Content-Encoding": "gzip"}
            ),
        )
        _raise_for_backpressure(response)
        response.raise_for_status()
        return ShipmentAck(ack_seq=_read_ack_seq(response))

    async def blob_present(self, blob_id: str) -> bool:
        response = await self._client.head(
            f"{self._base_url}/v1/blobs/{quote(blob_id, safe='')}", headers=self._headers()
        )
        if response.status_code == httpx.codes.NOT_FOUND:
            return False
        _raise_for_backpressure(response)
        if response.is_success:  # any 2xx (200, 204, …) means the plane holds this content
            return True
        response.raise_for_status()  # 4xx/5xx (and 3xx on httpx versions that raise for it)
        raise PlaneResponseError(
            f"unexpected status {response.status_code} for blob HEAD (neither 2xx nor 404)"
        )

    async def put_blob(self, blob_id: str, data: bytes) -> None:
        response = await self._client.put(
            f"{self._base_url}/v1/blobs/{quote(blob_id, safe='')}",
            content=data,
            headers=self._headers({"Content-Type": "application/octet-stream"}),
        )
        _raise_for_backpressure(response)
        response.raise_for_status()

    async def aclose(self) -> None:
        """Close the underlying client if this transport created it."""
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> HttpTransport:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()


def _encode_ndjson(shipment: Mapping[str, Any]) -> bytes:
    """Serialise a shipment as newline-delimited JSON: header line, then one event per line."""
    header = {key: value for key, value in shipment.items() if key != "events"}
    lines = [json.dumps(header, separators=(",", ":"))]
    lines.extend(json.dumps(event, separators=(",", ":")) for event in shipment["events"])
    return ("\n".join(lines) + "\n").encode("utf-8")


def _read_ack_seq(response: httpx.Response) -> int:
    """Read ``ack_seq`` from a plane's ``200`` body, or raise :class:`PlaneResponseError`.

    A body that is not JSON, or is missing a numeric ``ack_seq``, is a plane-contract
    violation — surfaced as the transport's own error rather than a bare ``KeyError`` /
    decode error escaping ``ship_run``.
    """
    try:
        return int(response.json()["ack_seq"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PlaneResponseError(f"plane response has no valid 'ack_seq': {exc}") from exc


def _raise_for_backpressure(response: httpx.Response) -> None:
    """Translate a ``429`` into :class:`BackpressureError`, parsing ``Retry-After`` if present."""
    if response.status_code == httpx.codes.TOO_MANY_REQUESTS:
        raise BackpressureError(
            "plane signalled backpressure (429)",
            retry_after=_parse_retry_after(response.headers.get("Retry-After")),
        )


def _parse_retry_after(value: str | None) -> float | None:
    """The ``Retry-After`` delay in seconds, or ``None`` to fall back to the shipper's backoff.

    Only the delta-seconds form is honoured; the HTTP-date form is left to the backoff rather
    than parsed, since the shipper only needs a lower bound on how long to wait. A non-finite
    (``inf``/``nan``) or negative value is rejected — a hostile or buggy plane must not be able
    to park the shipper forever on ``sleep(inf)`` — and falls back to the bounded backoff.
    """
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return seconds
