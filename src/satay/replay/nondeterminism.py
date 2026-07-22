"""Public error types for divergence and unsafe effects (N9/A10.2, ADR-0003/0006).

The replay engine is correct only for deterministic workflow bodies (ADR-0001). When
a replayed durable call does not match the journal at its position, the engine raises
:class:`NondeterminismError` carrying the *expected* versus *actual* call for a clear
message. Policy follows the effect-safety mode (ADR-0003): ``warn`` (dev) logs and
continues, ``strict`` raises. This is the same dev-warn / strict-fail policy model
V7 reuses for code-version mismatch.

:class:`EffectSafetyError` is raised in ``strict`` mode when a retryable
``side_effect=True`` task declares no idempotency or compensation strategy (ADR-0006).
"""

from __future__ import annotations


class NondeterminismError(RuntimeError):
    """Raised when a replayed durable call diverges from the journal (ADR-0003).

    Carries the ``position`` (global durable-call index), the ``expected`` call
    recorded at that position, and the ``actual`` call the replay issued, so the
    message names exactly what changed between runs.
    """

    def __init__(self, *, position: int, expected: str, actual: str) -> None:
        self.position = position
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"nondeterministic replay at durable-call position {position}: "
            f"journal expected {expected!r} but replay issued {actual!r} "
            f"(the workflow changed between runs)"
        )


class EffectSafetyError(RuntimeError):
    """Raised in ``strict`` mode for an unguarded retryable side-effecting task (ADR-0006).

    A ``@task(side_effect=True, retries>0)`` must declare an idempotency or
    compensation strategy — set ``@task(idempotent=True)`` or accept a ``ctx``
    parameter and guard the effect with ``ctx.idempotency_key``.
    """

    def __init__(self, task_name: str) -> None:
        self.task_name = task_name
        super().__init__(
            f"effect_safety=strict rejects task {task_name!r}: it is side-effecting and "
            f"retryable but declares no idempotency or compensation strategy. Set "
            f"@task(idempotent=True) or accept a ctx parameter and guard the effect with "
            f"ctx.idempotency_key."
        )
