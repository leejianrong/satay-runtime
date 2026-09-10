"""Producer-side journal ingest — the ``satay[ingest]`` extra (ADR-0044/0045).

Ship a local run's journal to a tier-1 ingest plane. This package is **in-tree but not part
of the runtime**: it is not re-exported from :mod:`satay`, nothing in the core imports it,
and deleting it leaves the runtime unchanged and releasable (ADR-0045 decision 1).

Importing this module pulls in only the pure shipper (:mod:`satay.ingest.shipper`), which
carries no third-party dependency. The concrete HTTP transport
(``satay.ingest.http``) needs the extra's HTTP client and is imported explicitly by a
caller that wants it, so ``import satay.ingest`` stays dependency-light.
"""

from __future__ import annotations

from satay.ingest.shipper import (
    BackpressureError,
    IngestError,
    MissingBlobError,
    RedactionRequiredError,
    ShipmentAck,
    ShippableStore,
    ShipResult,
    Transport,
    UnknownRunError,
    ship_run,
)

__all__ = [
    "BackpressureError",
    "IngestError",
    "MissingBlobError",
    "RedactionRequiredError",
    "ShipResult",
    "ShipmentAck",
    "ShippableStore",
    "Transport",
    "UnknownRunError",
    "ship_run",
]
