"""The redaction-enforcing read facade and the write facade (N15/N16/N18).

:class:`ReadAPI` wraps a store + a :class:`~satay.control.redaction.Redactor` and is
the **only** read path the HTTP server calls, so redaction is applied to every read
response — there is no code path that returns a run's data unredacted (N18). The raw
builders in :mod:`satay.control.views` stay redaction-free for structural unit tests.

:class:`ControlAPI` is the thin write facade: it allocates ids and enqueues commands
on the :class:`~satay.control.commands.CommandQueue`, returning immediately
(write-then-poll, ADR-0012). Both are pure Python — no FastAPI import here.
"""

from __future__ import annotations

import uuid
from typing import Any

from satay.control import commands, views
from satay.control.commands import CommandQueue
from satay.control.redaction import Redactor
from satay.journal import Store


class ReadAPI:
    """Journal-derived reads with mandatory read-time redaction (N16/N18)."""

    def __init__(self, store: Store, redactor: Redactor | None = None) -> None:
        self._store = store
        self._redactor = redactor or Redactor()

    async def run_list(self) -> dict[str, Any]:
        return self._redact(await views.run_list(self._store))

    async def timeline(self, run_id: str) -> dict[str, Any]:
        return self._redact(await views.timeline(self._store, run_id))

    async def tree(self, run_id: str) -> dict[str, Any]:
        return self._redact(await views.tree(self._store, run_id))

    async def task_detail(self, run_id: str, identity: str) -> dict[str, Any]:
        return self._redact(await views.task_detail(self._store, run_id, identity))

    async def compare(self, run_id: str, other_run_id: str) -> dict[str, Any]:
        return self._redact(await views.compare(self._store, run_id, other_run_id))

    def _redact(self, payload: dict[str, Any]) -> dict[str, Any]:
        redacted: dict[str, Any] = self._redactor.redact(payload)
        return redacted


class ControlAPI:
    """Enqueue control writes for the worker to apply (single-writer, ADR-0012)."""

    def __init__(self, store: Store, queue: CommandQueue) -> None:
        self._store = store
        self._queue = queue

    def start(
        self,
        workflow_name: str,
        workflow_input: Any = None,
        *,
        idempotency_key: str | None = None,
        run_id: str | None = None,
    ) -> str:
        """Enqueue a start; return the allocated run id (the worker drives it on a tick)."""
        resolved = run_id or uuid.uuid4().hex
        self._queue.submit(
            commands.StartRun(
                workflow_name=workflow_name,
                workflow_input=workflow_input,
                run_id=resolved,
                idempotency_key=idempotency_key,
            )
        )
        return resolved

    def cancel(self, run_id: str) -> None:
        """Enqueue a cancel (applied within one poll interval)."""
        self._queue.submit(commands.CancelRun(run_id=run_id))

    def send_event(
        self,
        event_type: str,
        *,
        key: str | None = None,
        payload: Any = None,
        run_id: str | None = None,
    ) -> None:
        """Enqueue an external event for delivery through the V3 inbox path."""
        self._queue.submit(
            commands.SendEvent(event_type=event_type, key=key, payload=payload, run_id=run_id)
        )

    async def validate_fork(self, source_run_id: str, fork_point_seq: int) -> None:
        """Validate a fork request now; execution is deferred to V7 (N15 stub)."""
        await commands.validate_fork_request(self._store, source_run_id, fork_point_seq)


__all__ = ["ControlAPI", "ReadAPI"]
