"""Payload spill to local files (A3.4, N19).

Payloads over the inline threshold (~256 KB, tunable) spill to a local filesystem
directory under ``./.satay/`` (ADR-0017) and are referenced from the journal by a blob
id. The same reference indirection admits a future object-store backend
(ARCHITECTURE §4.2).

Scaffold only: blob spill lands in V8.
"""

from __future__ import annotations
