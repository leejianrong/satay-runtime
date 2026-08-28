"""Reference-aware blob garbage collection: mark-and-sweep (ADR-0037, ADR-0039).

The **mark** phase lives on the store (:meth:`~satay.journal.store.SQLiteStore.
referenced_blob_ids`), since only the store can see a payload before blob-reference
rehydration resolves a spilled value away. This module is the **sweep**: it compares
that reference set against the ``.blob`` files actually on disk and deletes the ones
named by nobody, protected by a grace period rather than a lock held across the whole
pass (ADR-0037 Decision 3) — a blob spilled during or shortly before this sweep started
is kept regardless of whether the mark phase happened to see the run that wrote it.

Deliberately CLI-only (ADR-0039 Decision 2): this module is not re-exported from
``satay.__init__``, and the only caller is :mod:`satay.cli.main`. A destructive
operation gets no importable, unattended-scriptable entry point for its first cut.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from satay.blobs import BlobStore
from satay.journal import Store

#: Default grace-period buffer (ADR-0037 Decision 3), in seconds. A blob file is only
#: swept if its mtime is older than (the mark phase's own start time minus this
#: buffer) — the buffer widens the protected window rather than narrowing it, so a
#: blob spilled just before the mark phase started is protected too.
DEFAULT_GRACE_PERIOD_SECONDS = 300.0


@dataclass(frozen=True)
class GCReport:
    """The outcome of one GC pass — a dry run unless ``applied`` is set."""

    #: Blob ids still named by some run's journal.
    referenced_count: int
    #: Blob ids on disk, unreferenced, but within the grace period — not swept.
    protected_ids: list[str] = field(default_factory=list)
    #: Blob ids swept (or, on a dry run, that *would* be swept).
    reclaimable_ids: list[str] = field(default_factory=list)
    #: Total byte size of ``reclaimable_ids``.
    reclaimable_bytes: int = 0
    #: Total byte size of every ``.blob`` file that was not swept (referenced or
    #: within the grace period).
    kept_bytes: int = 0
    #: Whether files were actually deleted (``True``) or this was a dry run.
    applied: bool = False


async def collect_garbage(
    store: Store,
    blobs: BlobStore,
    *,
    apply: bool = False,
    grace_period_seconds: float = DEFAULT_GRACE_PERIOD_SECONDS,
    now: float | None = None,
) -> GCReport:
    """Run one mark-and-sweep pass. Dry run unless ``apply=True`` (ADR-0037 Decision 4).

    ``now`` overrides the mark phase's own start time (``time.time()`` by default) —
    for tests only; production callers never need it, since the grace period is
    measured against wall-clock file mtimes regardless.
    """
    mark_started_at = time.time() if now is None else now
    referenced = await store.referenced_blob_ids()
    cutoff = mark_started_at - grace_period_seconds

    protected_ids: list[str] = []
    reclaimable_ids: list[str] = []
    reclaimable_bytes = 0
    kept_bytes = 0

    directory = blobs.directory
    paths = sorted(directory.glob("*.blob")) if directory.exists() else []
    for path in paths:
        blob_id = path.stem
        size = path.stat().st_size
        if blob_id in referenced:
            kept_bytes += size
            continue
        if path.stat().st_mtime >= cutoff:
            protected_ids.append(blob_id)
            kept_bytes += size
            continue
        reclaimable_ids.append(blob_id)
        reclaimable_bytes += size
        if apply:
            path.unlink(missing_ok=True)

    return GCReport(
        referenced_count=len(referenced),
        protected_ids=protected_ids,
        reclaimable_ids=reclaimable_ids,
        reclaimable_bytes=reclaimable_bytes,
        kept_bytes=kept_bytes,
        applied=apply,
    )


__all__ = ["DEFAULT_GRACE_PERIOD_SECONDS", "GCReport", "collect_garbage"]
