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

from satay.api.decorators import task, workflow

#: In-process execution counts, keyed by task name.
EXECUTIONS: dict[str, int] = {}

#: Environment variable naming a JSON file that mirrors the counts across processes.
MARKER_ENV_VAR = "SATAY_DEMO_MARKER"


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
