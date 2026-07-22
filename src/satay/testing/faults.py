"""Fault injection (ADR-0011).

A first-class runtime affordance (not ad-hoc monkeypatching) for simulating a worker
crash or stall at a precise point: *after* a named journal event is committed. The
journal calls :meth:`FaultInjector.reached` after each commit; the injector then
raises a :class:`SimulatedCrash` ("crash after event X") or blocks until released
("stall after event X"), which lets a test prove the ADR-0012 property that reads keep
returning while the sole writer is stalled mid-write (Q51).
"""

from __future__ import annotations

import asyncio


class SimulatedCrash(RuntimeError):  # noqa: N818 — "crash" is the domain term (ADR-0011), not an error
    """Raised by the fault injector to simulate a worker crash after a commit point."""

    def __init__(self, event_type: str) -> None:
        super().__init__(f"simulated crash after event {event_type!r}")
        self.event_type = event_type


class FaultInjector:
    """Registry of faults keyed on journal event type.

    Register a fault, then let the journal drive :meth:`reached` after each committed
    event. A crash fires ``times`` times then clears itself; a stall blocks every
    matching commit until :meth:`release` is called.
    """

    def __init__(self) -> None:
        self._crash_after: dict[str, int] = {}
        self._stall_after: dict[str, asyncio.Event] = {}

    def crash_after(self, event_type: str, *, times: int = 1) -> None:
        """Arm a crash: the next ``times`` commits of ``event_type`` raise ``SimulatedCrash``."""
        if times < 1:
            raise ValueError("times must be >= 1")
        self._crash_after[event_type] = times

    def stall_after(self, event_type: str) -> asyncio.Event:
        """Arm a stall: commits of ``event_type`` block until the returned event is set.

        Returns the ``asyncio.Event`` gating the stall. Call ``.set()`` on it (or
        :meth:`release`) to let the stalled commit proceed.
        """
        gate = asyncio.Event()
        self._stall_after[event_type] = gate
        return gate

    def release(self, event_type: str) -> None:
        """Release a stall previously armed for ``event_type``."""
        gate = self._stall_after.get(event_type)
        if gate is not None:
            gate.set()

    async def reached(self, event_type: str) -> None:
        """Drive-point called by the journal after committing an event of ``event_type``.

        Raises :class:`SimulatedCrash` if a crash is armed for this type; otherwise
        awaits any armed stall gate.
        """
        remaining = self._crash_after.get(event_type)
        if remaining is not None:
            if remaining <= 1:
                del self._crash_after[event_type]
            else:
                self._crash_after[event_type] = remaining - 1
            raise SimulatedCrash(event_type)

        gate = self._stall_after.get(event_type)
        if gate is not None:
            await gate.wait()

    def clear(self) -> None:
        """Remove all armed faults."""
        self._crash_after.clear()
        for gate in self._stall_after.values():
            gate.set()
        self._stall_after.clear()
