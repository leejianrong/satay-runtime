"""Payload spill to local files (A3.4, N19, ADR-0004).

An encoded payload value larger than the inline threshold spills to a local file under
``./.satay/blobs/`` and the journal keeps only a **blob reference** — a tagged object
``{"$satay": "blobref", "id": <sha256>, "size": <bytes>}`` — in place of the inline
value. The reference sits behind the same ``input_ref`` / ``output_ref`` indirection V1
put in place, so nothing upstream of the store knows spill happened (the store spills on
write and rehydrates on read).

Blobs are **content-addressed** by the SHA-256 of their bytes, which makes them
immutable and free to share: a fork that copies a spilled payload re-derives the *same*
id and points at the *same* file, never rewriting or copying bytes (ADR-0004/Q54). The
threshold is pinned at **262144 bytes (256 KiB) on the encoded value** (ADR-0004, H3) so
the boundary is testable; it is still tunable via the ``threshold`` argument.

**Out of MVP scope (ADR-0004/Q54):** there is no run deletion and no compaction, so a
blob is never orphaned and there is deliberately **no blob GC / deletion here**. Blobs
accumulate under ``./.satay/`` and manual removal is the escape hatch; a future
retention / ``satay gc`` policy must be reference-aware because forks share blobs.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from satay.journal.codec import TAG_KEY

#: The spill threshold: an encoded value strictly larger than this many bytes spills to a
#: blob; a value at or below it stays inline (ADR-0004, H3, pinned exactly so the boundary
#: is testable).
SPILL_THRESHOLD_BYTES = 262144

#: The tagged-value kind marking a blob reference (shares the codec's ``$satay`` key so it
#: round-trips through JSON, but is resolved by the store, never by the codec).
BLOB_REF_KIND = "blobref"


class BlobResolutionError(RuntimeError):
    """Raised when a blob reference is read but no blob store is available to resolve it."""


def _dumps(value: Any) -> bytes:
    """Serialize an encoded value to its canonical compact JSON bytes."""
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


def encoded_size(value: Any) -> int:
    """Return the encoded-JSON byte length of an (already codec-encoded) value."""
    return len(_dumps(value))


def should_spill(value: Any, *, threshold: int = SPILL_THRESHOLD_BYTES) -> bool:
    """Whether an encoded value spills — strictly greater than the threshold does."""
    return encoded_size(value) > threshold


def make_blob_ref(blob_id: str, size: int) -> dict[str, Any]:
    """Build the tagged blob-reference object stored inline in place of a spilled value."""
    return {TAG_KEY: BLOB_REF_KIND, "id": blob_id, "size": size}


def is_blob_ref(value: Any) -> bool:
    """Whether ``value`` is a blob-reference object produced by :func:`make_blob_ref`."""
    return isinstance(value, Mapping) and value.get(TAG_KEY) == BLOB_REF_KIND


class BlobStore:
    """Content-addressed local blob store: bytes in, a stable SHA-256 id back.

    Files are named ``<sha256>.blob`` under the given directory. A ``put`` of identical
    bytes is a no-op that returns the same id (immutable + naturally deduplicated), which
    is exactly what makes a fork share a source blob rather than copy it (ADR-0004/Q54).
    The directory is created lazily on first write.
    """

    def __init__(self, directory: Path | str) -> None:
        self._dir = Path(directory)

    @property
    def directory(self) -> Path:
        """The directory blobs live in."""
        return self._dir

    def _path(self, blob_id: str) -> Path:
        return self._dir / f"{blob_id}.blob"

    def put(self, data: bytes) -> str:
        """Store ``data`` (if not already present) and return its content-address id."""
        blob_id = hashlib.sha256(data).hexdigest()
        path = self._path(blob_id)
        if not path.exists():
            self._dir.mkdir(parents=True, exist_ok=True)
            # Write to a temp file then atomically rename, so a reader never sees a
            # half-written blob and an existing blob is never rewritten (immutable).
            tmp = self._dir / f"{blob_id}.{id(data):x}.tmp"
            tmp.write_bytes(data)
            tmp.replace(path)
        return blob_id

    def get(self, blob_id: str) -> bytes:
        """Return the bytes for ``blob_id`` (raises ``FileNotFoundError`` if unknown)."""
        return self._path(blob_id).read_bytes()

    def has(self, blob_id: str) -> bool:
        """Whether a blob with ``blob_id`` exists on disk."""
        return self._path(blob_id).exists()


def spill_encoded(
    payload: Any,
    blobs: BlobStore,
    *,
    threshold: int = SPILL_THRESHOLD_BYTES,
) -> Any:
    """Spill over-threshold top-level values of an encoded payload mapping to blobs.

    Each top-level value (the ``input_ref`` / ``output_ref`` / ``event_ref`` etc. the
    journal carries) is measured on its own; a value larger than ``threshold`` is written
    to ``blobs`` and replaced by a blob reference, while a value at or below it is left
    inline. Non-mapping payloads pass through unchanged.
    """
    if not isinstance(payload, Mapping):
        return payload
    out: dict[str, Any] = {}
    for key, value in payload.items():
        data = _dumps(value)
        if len(data) > threshold:
            out[key] = make_blob_ref(blobs.put(data), len(data))
        else:
            out[key] = value
    return out


def rehydrate_encoded(payload: Any, blobs: BlobStore | None) -> Any:
    """Resolve any blob references in an encoded payload back to their inline values.

    The inverse of :func:`spill_encoded`: a spilled value rehydrates byte-for-byte to the
    same encoded structure an inline value would have had, so the codec (and everything
    above the store) cannot tell spill happened. Raises :class:`BlobResolutionError` if a
    reference is present but no blob store is available.
    """
    if not isinstance(payload, Mapping):
        return payload
    out: dict[str, Any] = {}
    for key, value in payload.items():
        if is_blob_ref(value):
            if blobs is None:
                raise BlobResolutionError(
                    f"payload field {key!r} is a spilled blob reference but no blob store "
                    f"is attached to resolve it"
                )
            out[key] = json.loads(blobs.get(str(value["id"])))
        else:
            out[key] = value
    return out


__all__ = [
    "BLOB_REF_KIND",
    "SPILL_THRESHOLD_BYTES",
    "BlobResolutionError",
    "BlobStore",
    "encoded_size",
    "is_blob_ref",
    "make_blob_ref",
    "rehydrate_encoded",
    "should_spill",
    "spill_encoded",
]
