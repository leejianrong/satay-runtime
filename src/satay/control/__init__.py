"""Control and read API (A7/A8, N15/N16/N18).

An HTTP server on its **own thread** exposing write endpoints (start, status, cancel,
send_event, fork) and read endpoints (run list, timeline, tree, task/attempt detail,
compare). Reads hit SQLite directly over read-only connections; writes are enqueued on
the in-process command queue and applied by the worker, which stays the single writer
(ADR-0012). The redactor is a read-time transform, guarded by a per-session token and
an Origin/Host allow-list (ADR-0014).

This whole stack (FastAPI + uvicorn + Pydantic response models) ships **only in the
``satay[studio]`` extra**, never the core (ADR-0013). Scaffold only: the API lands in
V5. Do not import FastAPI/uvicorn/Pydantic at core import time.
"""

from __future__ import annotations
