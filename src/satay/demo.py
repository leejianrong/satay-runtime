"""The V1 two-task demo workflow with an execution-count marker (build-plan step 13).

``demo(value)`` awaits ``step_one`` then ``step_two``. Each task bumps an
execution-count marker on *real* execution, so "reused" versus "re-executed" is
observable (ADR-0011): a reused result adds nothing to the count, a re-run increments
it. The marker is in-memory by default (the E2E seam runs in-process) and file-backed
when ``SATAY_DEMO_MARKER`` names a path, so a real crash-and-restart demo across
processes can still observe counts.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from satay.api.context import task_context
from satay.api.decorators import task, workflow
from satay.api.primitives import gather, sleep, start_child, wait_for_event
from satay.api.primitives import map as satay_map

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

#: Live/peak in-flight gauge for the V4 ``map`` concurrency-bound demo (ADR-0007).
CONCURRENCY_GAUGE: dict[str, int] = {"current": 0, "peak": 0}


def reset_concurrency_gauge() -> None:
    """Reset the map concurrency gauge (called from :func:`reset_executions`)."""
    CONCURRENCY_GAUGE["current"] = 0
    CONCURRENCY_GAUGE["peak"] = 0


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
    FORK_STEP_BUMP["amount"] = 1
    reset_concurrency_gauge()
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


# -- V7 demo: fork under a changed task impl -------------------------------------

#: The "changed code" knob for the fork demo. ``fork_step`` reads it, so a test (or the
#: demo) can flip it between the source run and its fork to prove the fork re-runs the
#: downstream under new behaviour while the source stays byte-for-byte unchanged.
FORK_STEP_BUMP: dict[str, int] = {"amount": 1}


@task()
async def fork_step(value: int) -> int:
    """Add the current ``FORK_STEP_BUMP`` amount (marks a real execution).

    The downstream task the fork re-runs: change ``FORK_STEP_BUMP`` and re-run under a
    fork to see a different result, with ``step_one`` reused as a journal hit.
    """
    record_execution("fork_step")
    return value + FORK_STEP_BUMP["amount"]


@workflow
async def fork_demo(value: int) -> int:
    """``step_one`` (reused across a fork) then ``fork_step`` (re-run under changed code)."""
    first = await step_one(value)
    return await fork_step(first)


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


# -- V3 demo: timers and events --------------------------------------------------

#: The event key the review workflows wait on (a single, deterministic value).
REVIEW_KEY = "review-1"


@dataclass(frozen=True)
class ReviewDecision:
    """An external decision delivered via ``satay.send_event`` (V3 demo event type)."""

    approved: bool
    reviewer: str = ""


@workflow
async def sleep_demo(value: int) -> int:
    """Run ``step_one``, durably sleep, then ``step_two`` — parks across the sleep.

    Proves the release-while-waiting path: after ``step_one`` the run parks on the
    ``sleep`` timer (no live frame) and resumes when the worker fires it, reusing the
    recorded ``step_one`` result on the wake.
    """
    first = await step_one(value)
    await sleep(timedelta(hours=1))
    return await step_two(first)


@workflow
async def review_demo(value: int) -> str:
    """Block on ``wait_for_event(ReviewDecision, key=…)`` and act on the delivery."""
    decision = await wait_for_event(ReviewDecision, key=REVIEW_KEY)
    return "approved" if decision.approved else "rejected"


@workflow
async def review_timeout_demo(value: int) -> str:
    """Wait for a ``ReviewDecision`` bounded by a timeout; ``None`` means it timed out."""
    decision = await wait_for_event(ReviewDecision, key=REVIEW_KEY, timeout=timedelta(hours=2))
    if decision is None:
        return "timed_out"
    return "approved" if decision.approved else "rejected"


# -- V4 demo: composite primitives and parallel crash-recovery -------------------


def item_key(value: int) -> str:
    """The stable fan-out key for a mapped integer item (its own value, ``item-N``)."""
    return f"item-{value}"


@task()
async def square_item(value: int) -> int:
    """Square one mapped item, marking a per-key real execution (reuse vs re-run marker)."""
    record_execution(item_key(value))
    return value * value


@workflow
async def map_square_demo(values: list[int]) -> list[int]:
    """Fan out ``square_item`` over keyed items (sequential bound for a deterministic crash).

    The signature demo: crash mid-fan-out, then on restart only unresolved items re-run.
    ``concurrency=1`` makes the crash point deterministic (exactly the items whose
    ``TaskCompleted`` was recorded survive); the parallel bound is proven separately.
    """
    return await satay_map(square_item, values, key=item_key, concurrency=1)


@task()
async def gauge_item(value: int) -> int:
    """Bump a live in-flight gauge, yield a few times, then return — proves the bound (N5)."""
    CONCURRENCY_GAUGE["current"] += 1
    CONCURRENCY_GAUGE["peak"] = max(CONCURRENCY_GAUGE["peak"], CONCURRENCY_GAUGE["current"])
    for _ in range(3):
        await asyncio.sleep(0)  # force interleaving so concurrent items overlap
    CONCURRENCY_GAUGE["current"] -= 1
    return value + 1


@workflow
async def bounded_map_demo(values: list[int]) -> list[int]:
    """Fan out ``gauge_item`` with an explicit ``concurrency=2`` bound."""
    return await satay_map(gauge_item, values, key=item_key, concurrency=2)


@workflow
async def default_bound_map_demo(values: list[int]) -> list[int]:
    """Fan out ``gauge_item`` with the default (unspecified) concurrency bound."""
    return await satay_map(gauge_item, values, key=item_key)


@task()
async def add_hundred(value: int) -> int:
    """A plain durable task (a heterogeneous ``gather`` member alongside a ``map``)."""
    record_execution("add_hundred")
    return value + 100


@workflow
async def gather_demo(value: int) -> list[object]:
    """Gather a scalar task result and a nested map result, rejoined **positionally**."""
    results = await gather(
        add_hundred(value),
        satay_map(square_item, [1, 2, 3], key=item_key, concurrency=3),
    )
    return results


@task()
async def maybe_boom(value: int) -> int:
    """Fail fast on item ``2``; other items mark a real execution and return (ADR-0020)."""
    record_execution(item_key(value))
    if value == 2:
        raise RuntimeError("map item 2 boom")
    return value


@workflow
async def failing_map_demo(values: list[int]) -> list[int]:
    """A ``map`` where one item fails — the whole map raises (fail-fast, ADR-0020)."""
    return await satay_map(maybe_boom, values, key=item_key, concurrency=3)


# -- V4 demo: child workflows ----------------------------------------------------


@task()
async def child_task(value: int) -> int:
    """The child workflow's single task (marks a real execution)."""
    record_execution("child_task")
    return value * 10


@workflow
async def child_workflow(value: int) -> int:
    """A simple child workflow: one task, ``value * 10``."""
    return await child_task(value)


@workflow
async def parent_workflow(value: int) -> int:
    """Start a linked child, await its result, and add one (proves child linkage + reuse)."""
    handle = await start_child(child_workflow, value)
    child_result: int = await handle.result()
    return child_result + 1


@task()
async def child_boom(value: int) -> int:
    """A child task that always fails (marks a real execution before raising)."""
    record_execution("child_boom")
    raise RuntimeError("child workflow boom")


@workflow
async def failing_child_workflow(value: int) -> int:
    """A child workflow whose task fails — surfaces to the parent as a raised error."""
    return await child_boom(value)


@workflow
async def parent_of_failing_child(value: int) -> int:
    """Start a child that fails; the failure surfaces here as a raised exception (ADR-0020)."""
    handle = await start_child(failing_child_workflow, value)
    result: int = await handle.result()
    return result


@task()
async def child_step_a(value: int) -> int:
    """First task of the two-step child (marks a real execution)."""
    record_execution("child_step_a")
    return value + 1


@task()
async def child_step_b(value: int) -> int:
    """Second task of the two-step child (marks a real execution)."""
    record_execution("child_step_b")
    return value * 2


@workflow
async def two_step_child(value: int) -> int:
    """A two-task child, so a crash between its tasks leaves it resumable mid-flight."""
    first = await child_step_a(value)
    return await child_step_b(first)


@workflow
async def parent_of_two_step_child(value: int) -> int:
    """Start a two-step child; a crash mid-child resumes (not restarts) on parent resume."""
    handle = await start_child(two_step_child, value)
    result: int = await handle.result()
    return result
