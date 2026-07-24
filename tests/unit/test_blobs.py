"""Unit tests for blob spill (N19, ADR-0004): the boundary, ref symmetry, immutability."""

from __future__ import annotations

from pathlib import Path

from satay.blobs import (
    SPILL_THRESHOLD_BYTES,
    BlobStore,
    encoded_size,
    is_blob_ref,
    make_blob_ref,
    rehydrate_encoded,
    should_spill,
    spill_encoded,
)
from satay.journal.codec import encode


def test_threshold_is_pinned_at_262144() -> None:
    assert SPILL_THRESHOLD_BYTES == 262144


def test_spill_decision_fires_exactly_at_the_boundary() -> None:
    """At or just below 262144 encoded bytes stays inline; just above spills (ADR-0004 H3)."""
    # A JSON string of length L encodes to L + 2 bytes (the surrounding quotes).
    at_threshold = "x" * (SPILL_THRESHOLD_BYTES - 2)
    just_below = "x" * (SPILL_THRESHOLD_BYTES - 3)
    just_above = "x" * (SPILL_THRESHOLD_BYTES - 1)

    assert encoded_size(at_threshold) == SPILL_THRESHOLD_BYTES
    assert encoded_size(just_above) == SPILL_THRESHOLD_BYTES + 1

    assert should_spill(just_below) is False
    assert should_spill(at_threshold) is False  # at the boundary: inline
    assert should_spill(just_above) is True  # one byte over: spill


def test_spill_encoded_replaces_only_over_threshold_values(tmp_path: Path) -> None:
    blobs = BlobStore(tmp_path / "blobs")
    big = "x" * (SPILL_THRESHOLD_BYTES + 100)
    payload = {"output_ref": encode(big), "small": encode("hello")}

    spilled = spill_encoded(payload, blobs)

    assert is_blob_ref(spilled["output_ref"])  # the big value became a reference
    assert spilled["small"] == "hello"  # the small value stayed inline
    # The referenced blob really landed on disk.
    assert blobs.has(spilled["output_ref"]["id"])


def test_blob_reference_round_trips_symmetrically(tmp_path: Path) -> None:
    """A spilled value rehydrates byte-for-byte to what an inline value would have been."""
    blobs = BlobStore(tmp_path / "blobs")
    original = {"deep": {"list": list(range(50_000))}}
    encoded = {"output_ref": encode(original)}

    spilled = spill_encoded(encoded, blobs)
    assert is_blob_ref(spilled["output_ref"])

    rehydrated = rehydrate_encoded(spilled, blobs)
    assert rehydrated == encoded  # identical to the never-spilled encoded payload


def test_blob_store_is_content_addressed_and_immutable(tmp_path: Path) -> None:
    """Identical bytes yield the same id (dedup) and the blob is never rewritten (Q54)."""
    blobs = BlobStore(tmp_path / "blobs")
    data = b'{"big":"payload"}'

    id_a = blobs.put(data)
    written_path = blobs.directory / f"{id_a}.blob"
    mtime_before = written_path.stat().st_mtime_ns

    id_b = blobs.put(data)  # same content
    assert id_a == id_b
    # No rewrite: the existing immutable blob file was left untouched.
    assert written_path.stat().st_mtime_ns == mtime_before

    # Different content is a different blob.
    id_c = blobs.put(b'{"other":"payload"}')
    assert id_c != id_a


def test_make_and_detect_blob_ref() -> None:
    ref = make_blob_ref("abc123", 999)
    assert is_blob_ref(ref)
    assert ref["id"] == "abc123"
    assert ref["size"] == 999
    assert not is_blob_ref({"just": "a dict"})
    assert not is_blob_ref("a string")
