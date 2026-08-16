"""Name-keyed registry of workflow and task definitions (N1/N2).

A single process-global registry backs both replay matching (identity is by task
*name*, ADR-0002) and code-version source hashing (N17). Definitions are keyed by
name; a duplicate name is rejected so two tasks can never share a durable identity.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class WorkflowDefinition:
    """A registered ``@satay.workflow``: its name and the underlying coroutine fn."""

    name: str
    fn: Callable[..., Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class TaskDefinition:
    """A registered ``@satay.task``.

    ``retries`` / ``timeout`` drive the V2 retry loop (N10); ``side_effect`` /
    ``idempotent`` drive effect-safety enforcement (A10.2).
    """

    name: str
    fn: Callable[..., Awaitable[Any]]
    retries: int = 0
    timeout: float | None = None
    side_effect: bool = False
    idempotent: bool = False

    @property
    def is_effect_guarded(self) -> bool:
        """Whether a retryable side-effecting task declares an idempotency strategy.

        Guarded means the author flagged ``idempotent=True`` — a promise that the body
        keys its effect on ``ctx.idempotency_key`` (or otherwise compensates). Unguarded
        retryable side effects are rejected under ``effect_safety=strict`` (ADR-0006).

        Guarded is guarded **within one run**: ``ctx.idempotency_key`` embeds the
        ``run_id``, so this flag says nothing about a re-trigger, which the engine checks
        separately and only ever warns about (``ReplayEngine._warn_unnameable_run``). See
        :attr:`satay.TaskContext.idempotency_key` for both traps (KAN-476).
        """
        return self.idempotent


class Registry:
    """Process-global registry of workflow and task definitions."""

    def __init__(self) -> None:
        self._workflows: dict[str, WorkflowDefinition] = {}
        self._tasks: dict[str, TaskDefinition] = {}

    def register_workflow(self, definition: WorkflowDefinition) -> None:
        existing = self._workflows.get(definition.name)
        if existing is not None and existing.fn is not definition.fn:
            raise ValueError(f"workflow name {definition.name!r} already registered")
        self._workflows[definition.name] = definition

    def register_task(self, definition: TaskDefinition) -> None:
        existing = self._tasks.get(definition.name)
        if existing is not None and existing.fn is not definition.fn:
            raise ValueError(f"task name {definition.name!r} already registered")
        self._tasks[definition.name] = definition

    def get_workflow(self, name: str) -> WorkflowDefinition | None:
        return self._workflows.get(name)

    def get_task(self, name: str) -> TaskDefinition | None:
        return self._tasks.get(name)

    def workflow_names(self) -> list[str]:
        return sorted(self._workflows)

    def task_names(self) -> list[str]:
        return sorted(self._tasks)

    def iter_source_targets(self) -> list[Callable[..., Awaitable[Any]]]:
        """All registered callables, name-sorted — the source-hash input for N17."""
        pairs: list[tuple[str, Callable[..., Awaitable[Any]]]] = []
        pairs += [(f"workflow:{n}", d.fn) for n, d in self._workflows.items()]
        pairs += [(f"task:{n}", d.fn) for n, d in self._tasks.items()]
        return [fn for _, fn in sorted(pairs, key=lambda p: p[0])]


#: The process-global registry the decorators write to and the engine reads.
REGISTRY = Registry()
