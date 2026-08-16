"""Public error types for divergence and unsafe effects (N9/A10.2, ADR-0003/0006).

The replay engine is correct only for deterministic workflow bodies (ADR-0001). When
a replayed durable call does not match the journal at its position, the engine raises
:class:`NondeterminismError` carrying the *expected* versus *actual* call for a clear
message. Policy follows :class:`~satay.config.NondeterminismPolicy`, which defaults to
``strict`` and raises (ADR-0003/0022); ``warn`` logs and continues, which lets the run
finish with a wrong result, and ``off`` does so silently.

:class:`EffectSafetyError` is raised in ``strict`` mode when a retryable
``side_effect=True`` task declares no idempotency or compensation strategy (ADR-0006).
That is a **separate** setting, :class:`~satay.config.EffectSafety`, which keeps its
``warn`` default; the two share a vocabulary but not a risk profile.
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
    compensation strategy — set ``@task(idempotent=True)`` and key the effect on
    ``ctx.idempotency_key``.

    The message names the *whole* fix, not just this task's half (KAN-476).
    ``ctx.idempotency_key`` embeds the ``run_id``, so it deduplicates retries and
    resumes of one run; surviving a **re-trigger** additionally needs the run itself to
    be keyed, with ``satay.start(..., idempotency_key=...)``. A developer who does only
    what this error asks still double-loads on the second trigger, which is why the
    sentence is here rather than only in the docs.
    """

    def __init__(self, task_name: str) -> None:
        self.task_name = task_name
        super().__init__(
            f"effect_safety=strict rejects task {task_name!r}: it is side-effecting and "
            f"retryable but declares no idempotency or compensation strategy. Set "
            f"@task(idempotent=True) and key the effect on ctx.idempotency_key. That key "
            f"covers retries and resumes of THIS run; to survive a re-trigger of the same "
            f"work, start the run with satay.start(..., idempotency_key=...) too."
        )
