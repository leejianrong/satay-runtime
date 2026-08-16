"""Task-facing context (``TaskContext``, N14).

A running task reads its context with :func:`task_context`, which returns the
:class:`TaskContext` bound for the current attempt. The context carries the durable
identity of the attempt and the opt-in model-usage recording slot (ADR-0008). A task
reads ``ctx.idempotency_key`` — stable across retries, distinct across invocations
(A4.3) — to make external effects safe under at-least-once execution. What that key
covers, and the two things it silently does not (a re-trigger, and a multi-row effect),
are spelled out on :attr:`TaskContext.idempotency_key` itself.

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
        """The stable idempotency key of this logical task (read-only, A4.3).

        ``sha256(run_id, task_name, ordinal-or-map-key)``. Arguments are excluded, so it
        is identical across every physical attempt of one logical call and different for
        every other call, map item, and run. Key an external effect on it and
        at-least-once execution stops being able to duplicate that effect.

        Two things it does **not** do. Both fail silently, and neither is a bug you can
        find by reading a stack trace (KAN-476).

        **It embeds the run id, so it does not survive a re-trigger.** It deduplicates
        retries and resumes *of this run*. Run the same logical work again — an operator
        re-running last night's load — and ``satay.start`` mints a fresh ``run_id``, so
        every key here changes and every effect lands a second time. ``idempotent=True``
        stays true and the runtime stays quiet, because at the task level nothing is
        wrong. What closes it is keying the **run** as well as the effect::

            # the trigger: a repeated key resolves to the same run instead of a new one
            satay.start(nightly_load, sources, idempotency_key="load-2026-08-16")

            # the effect: keyed on ctx, which is now stable across that re-trigger
            @satay.task(side_effect=True, retries=2, idempotent=True)
            async def load(batch: Batch) -> int:
                ctx = satay.task_context()
                return await warehouse.insert_or_ignore(key=ctx.idempotency_key, ...)

        A run started without that key gets a warning naming this, once per task per
        drive, unless ``effect_safety='off'``.

        **It identifies one durable call, not one row.** A call that writes N rows needs
        N distinct dedupe keys and has to compose them itself, conventionally with a
        separator::

            for row in batch.rows:
                await warehouse.insert_or_ignore(
                    key=f"{ctx.idempotency_key}#{row.record_id}", body=row.body
                )

        Write the bare key as the unique column on a four-row batch and the first insert
        wins, the other three are silently ignored as duplicates of it, and the task
        returns success having loaded one row of four. **The runtime cannot detect this
        and will never warn about it** — the composition happens inside your effect,
        which Satay does not see; only your own row counts can catch it. If your effect
        writes more than one thing, compose the key per thing.
        """
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
