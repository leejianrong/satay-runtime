"""Task-facing context (``TaskContext``).

The object injected into a task body, exposing per-attempt metadata and the
model-usage recording slot. Concrete behaviour lands in V1 (attempt/idempotency in
V2); this is the public shape.
"""

from __future__ import annotations

from typing import Any


class TaskContext:
    """Injected into a running task. Public surface; behaviour lands in V1.

    Carries the durable identity of the current task attempt and the schemaless
    ``record_model_usage`` slot that reaches the journal (ARCHITECTURE §5).
    """

    run_id: str
    task_name: str
    attempt: int

    def record_model_usage(self, **usage: Any) -> None:
        """Record schemaless model-usage for this attempt (lands in V1)."""
        raise NotImplementedError("TaskContext.record_model_usage lands in V1")
