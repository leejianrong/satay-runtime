"""The V1 two-task demo workflow with an execution-count marker (build-plan step 13).

``demo(value)`` awaits ``step_one`` then ``step_two``. Each task bumps an
execution-count marker on *real* execution, so "reused" versus "re-executed" is
observable (ADR-0011): a reused result adds nothing to the count, a re-run increments
it. The marker is in-memory by default (the E2E seam runs in-process) and file-backed
when ``SATAY_DEMO_MARKER`` names a path, so a real crash-and-restart demo across
processes can still observe counts.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from satay.api.context import task_context
from satay.api.decorators import task, workflow

#: In-process execution counts, keyed by task name.
EXECUTIONS: dict[str, int] = {}

#: Environment variable naming a JSON file that mirrors the counts across processes.
MARKER_ENV_VAR = "SATAY_DEMO_MARKER"

#: Idempotency keys whose guarded side effect has already been applied. Stands in for a
#: durable idempotency-guard store (a DB row keyed by ``ctx.idempotency_key`` in real
#: use); in-memory here because the E2E seam crashes and restarts in one process, just
#: like the execution-count marker (ADR-0011).
SIDE_EFFECTS_DONE: set[str] = set()

#: Physical-attempt counter for the fail-twice-then-succeed demo task.
FLAKY_ATTEMPTS: dict[str, int] = {}


def _marker_path() -> Path | None:
    value = os.environ.get(MARKER_ENV_VAR)
    return Path(value) if value else None


def record_execution(name: str) -> None:
    """Increment the execution count for ``name`` (in-memory and, if set, on disk)."""
    EXECUTIONS[name] = EXECUTIONS.get(name, 0) + 1
    path = _marker_path()
    if path is not None:
        counts = _read_file(path)
        counts[name] = counts.get(name, 0) + 1
        path.write_text(json.dumps(counts))


def execution_count(name: str) -> int:
    """Return the recorded execution count for ``name`` (prefers the on-disk file)."""
    path = _marker_path()
    if path is not None:
        return _read_file(path).get(name, 0)
    return EXECUTIONS.get(name, 0)


def reset_executions() -> None:
    """Clear the in-memory counts and remove any on-disk marker file."""
    EXECUTIONS.clear()
    SIDE_EFFECTS_DONE.clear()
    FLAKY_ATTEMPTS.clear()
    path = _marker_path()
    if path is not None and path.exists():
        path.unlink()


def _read_file(path: Path) -> dict[str, int]:
    if not path.exists():
        return {}
    result: dict[str, int] = json.loads(path.read_text())
    return result


@task()
async def step_one(value: int) -> int:
    """First durable task: increments the input (marks a real execution)."""
    record_execution("step_one")
    return value + 1


@task()
async def step_two(value: int) -> int:
    """Second durable task: doubles the input (marks a real execution)."""
    record_execution("step_two")
    return value * 2


@workflow
async def demo(value: int) -> int:
    """Await ``step_one`` then ``step_two``; return the composed result."""
    first = await step_one(value)
    second = await step_two(first)
    return second


# -- V2 demo tasks ---------------------------------------------------------------


@task(retries=2)
async def flaky_thrice(value: int) -> int:
    """Fail on the first two physical attempts, succeed on the third (N10).

    Every attempt marks a real execution, so the timeline shows three attempts and the
    execution count reaches 3.
    """
    record_execution("flaky_thrice")
    n = FLAKY_ATTEMPTS.get("flaky_thrice", 0) + 1
    FLAKY_ATTEMPTS["flaky_thrice"] = n
    if n < 3:
        raise RuntimeError(f"flaky_thrice transient failure #{n}")
    return value + 1


@workflow
async def flaky_demo(value: int) -> int:
    """A one-task workflow whose task fails twice then succeeds (three attempts)."""
    return await flaky_thrice(value)


@task(side_effect=True, retries=2, idempotent=True)
async def interrupted_effect(value: int) -> int:
    """A side effect guarded by ``ctx.idempotency_key`` (N4/N13/N14).

    The effect is applied at most once per logical task (keyed on the stable
    idempotency key). The first attempt fails *after* applying the effect, so a
    retry (or a crash-and-restart before ``TaskCompleted``) re-runs the body — and the
    key guard makes the effect run exactly once across that at-least-once re-execution.
    """
    ctx = task_context()
    if ctx.idempotency_key not in SIDE_EFFECTS_DONE:
        SIDE_EFFECTS_DONE.add(ctx.idempotency_key)
        record_execution("interrupted_effect_applied")  # the real, once-only effect
    record_execution("interrupted_effect_body")  # every physical attempt
    if ctx.attempt == 1:
        raise RuntimeError("transient failure after the side effect ran")
    return value + 1


@workflow
async def interrupted_effect_demo(value: int) -> int:
    """A one-task workflow whose side effect is key-guarded against re-runs."""
    return await interrupted_effect(value)


@task()
async def nd_first(value: int) -> int:
    """First task of the reorder fixture (marks a real execution)."""
    record_execution("nd_first")
    return value + 1


@task()
async def nd_second(value: int) -> int:
    """Second task of the reorder fixture (marks a real execution)."""
    record_execution("nd_second")
    return value * 2


@workflow
async def reorder_original(value: int) -> int:
    """The original call order: ``nd_first`` then ``nd_second``."""
    first = await nd_first(value)
    return await nd_second(first)


@workflow
async def reorder_edited(value: int) -> int:
    """The edited body with calls reordered — replay raises ``NondeterminismError`` (N9)."""
    second = await nd_second(value)
    return await nd_first(second)


@task(side_effect=True, retries=1)
async def unguarded_effect(value: int) -> int:
    """A retryable side-effecting task with no idempotency strategy (A10.2).

    Rejected at schedule time under ``effect_safety=strict``; a warning under ``warn``.
    """
    record_execution("unguarded_effect")
    return value + 1


@workflow
async def unguarded_effect_demo(value: int) -> int:
    """A one-task workflow with an unguarded retryable side effect."""
    return await unguarded_effect(value)


@task()
async def usage_task(value: int) -> int:
    """A task that self-reports model usage into the generic usage slot (N14, ADR-0008)."""
    task_context().record_model_usage(model="demo-model", input_tokens=10, output_tokens=5)
    record_execution("usage_task")
    return value + 1


@workflow
async def usage_demo(value: int) -> int:
    """A one-task workflow whose task self-reports model usage."""
    return await usage_task(value)


@task()
async def quiet_task(value: int) -> int:
    """A task that reports no usage (proves a non-reporting task records none)."""
    record_execution("quiet_task")
    return value + 1


@workflow
async def quiet_demo(value: int) -> int:
    """A one-task workflow whose task reports no model usage."""
    return await quiet_task(value)
