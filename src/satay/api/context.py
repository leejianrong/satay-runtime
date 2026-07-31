"""Task-facing context (``TaskContext``, N14).

A running task reads its context with :func:`task_context`, which returns the
:class:`TaskContext` bound for the current attempt. The context carries the durable
identity of the attempt and the opt-in model-usage recording slot (ADR-0008). A task
reads ``ctx.idempotency_key`` — stable across retries, distinct across invocations
(A4.3) — to make external effects safe under at-least-once execution.

Injection is via a ``ContextVar`` (the same pattern the replay driver uses), so task
signatures stay ordinary and tasks remain independently callable. Usage is buffered on
the context and flushed by the executor into the journal's generic usage slot on the
attempt's outcome event — ``TaskCompleted`` **or** ``TaskAttemptFailed``, since a failed
attempt was billed too (KAN-479). The context is per attempt, so a task need not do
anything to have its retries priced. The core ships no model adapters: a task that never
calls :meth:`record_model_usage` records no usage.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any


class TaskContext:
    """The context bound for one task attempt (N14)."""

    def __init__(
        self,
        *,
        run_id: str,
        task_name: str,
        ordinal: int,
        attempt: int,
        idempotency_key: str,
    ) -> None:
        self.run_id = run_id
        self.task_name = task_name
        self.ordinal = ordinal
        self.attempt = attempt
        self._idempotency_key = idempotency_key
        self._usage: list[dict[str, Any]] = []

    @property
    def idempotency_key(self) -> str:
        """The stable idempotency key of this logical task (read-only, A4.3)."""
        return self._idempotency_key

    def record_model_usage(
        self,
        model: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        **extra: Any,
    ) -> None:
        """Record schemaless model usage for this attempt (opt-in self-report, ADR-0008).

        Only the fields supplied are stored, plus any ``extra`` (so non-LLM cost or
        provider-specific fields ride along). The executor flushes the buffer into the
        journal's usage slot on this attempt's outcome — ``TaskCompleted`` if it succeeds,
        ``TaskAttemptFailed`` if it does not, so recording before the call that might fail
        is what gets a retried task priced honestly. Studio renders it in V6.
        """
        entry: dict[str, Any] = dict(extra)
        if model is not None:
            entry["model"] = model
        if input_tokens is not None:
            entry["input_tokens"] = input_tokens
        if output_tokens is not None:
            entry["output_tokens"] = output_tokens
        self._usage.append(entry)

    @property
    def recorded_usage(self) -> list[dict[str, Any]]:
        """This attempt's usage entries (executor-internal; empty if none)."""
        return list(self._usage)


#: The context bound for the current task attempt, or ``None`` outside a task body.
CURRENT_TASK_CONTEXT: ContextVar[TaskContext | None] = ContextVar(
    "satay_task_context", default=None
)


def task_context() -> TaskContext:
    """Return the :class:`TaskContext` for the currently running task (N14).

    Raises :class:`RuntimeError` if called outside a ``@satay.task`` body.
    """
    ctx = CURRENT_TASK_CONTEXT.get()
    if ctx is None:
        raise RuntimeError("task_context() must be called inside a running @satay.task body")
    return ctx
