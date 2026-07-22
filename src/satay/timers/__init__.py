"""Timers and events (A5, N11).

Persists timer rows and an event inbox, and runs the poll loop (~1s in dev) that
fires due timers and delivers events, resuming waiting runs. An asyncio background
task over the store, using the same **injected clock** as the executor
(ARCHITECTURE §3.5). A matching event wins over a simultaneously-due timeout, and
buffered matches are consumed FIFO by ``received_at`` (D22, ADR-0021).

Scaffold only: the poll loop, timers, and event inbox land in V3.
"""

from __future__ import annotations
