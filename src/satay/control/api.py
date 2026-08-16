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
        """Align two runs by durable-call identity.

        **The wire spelling of ``other_run_id`` is ``to``**: the HTTP route is
        ``GET /runs/{run_id}/compare?to={other_run_id}`` and the query parameter is
        required, so ``?other_run_id=`` returns 422 (KAN-490). The names diverge on
        purpose — ``to`` reads as prose in a URL the path has already scoped to runs,
        ``other_run_id`` reads as a Python argument — so anything quoting a compare URL
        must use ``to``.
        """
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

    async def fork(
        self,
        source_run_id: str,
        fork_point_seq: int | None = None,
        *,
        before_task: str | None = None,
        before_ordinal: int | None = None,
        workflow_input: Any = commands.INHERIT,
    ) -> str:
        """Validate a fork request, enqueue it, and return the new run id (N15, V7).

        Validation is synchronous (an unknown/non-terminal source or a bad fork point
        raises :class:`~satay.control.commands.ForkValidationError` → HTTP 400 before
        anything is enqueued). The new run id is allocated here so the HTTP caller can
        navigate to it immediately; the worker seeds and drives it on its next poll tick
        (write-then-poll, single writer, ADR-0012).

        The fork point is either an explicit ``fork_point_seq`` (what Studio sends, from
        a clicked event) or a ``before_task=`` name resolved by
        :func:`~satay.control.commands.resolve_fork_point`. ``workflow_input=`` runs the
        fork under a different input (KAN-481, ADR-0028).
        """
        resolved_seq = await commands.resolve_fork_point(
            self._store,
            source_run_id,
            fork_point_seq=fork_point_seq,
            before_task=before_task,
            before_ordinal=before_ordinal,
        )
        new_run_id = uuid.uuid4().hex
        self._queue.submit(
            commands.ForkRun(
                source_run_id=source_run_id,
                fork_point_seq=resolved_seq,
                run_id=new_run_id,
                workflow_input=workflow_input,
            )
        )
        return new_run_id


__all__ = ["ControlAPI", "ReadAPI"]
