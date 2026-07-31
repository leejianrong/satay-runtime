"""Resumed results must have the same Python type as first-execution results (KAN-474).

Driven through the primary seam (ADR-0011): the public ``satay.start`` API, a temp
``SQLiteStore``, and the ``FaultInjector`` crash hook. Each workflow runs twice against
the same store — once cleanly (first execution, the plain Python return value) and once
with a crash armed right after the single task's ``TaskCompleted`` commit, so the resumed
attempt *rehydrates* that result from the journal instead of re-running the task.

The assertion is **type identity**, not field values: a plain ``dict`` can carry the
right fields and still be the wrong type, which is exactly the latent
``AttributeError``-on-recovery bug this guards. The observable is a type-shape string
computed inside the workflow body from the value the runtime handed it, returned as the
workflow's (``str``) output — no private replay internals are touched.
"""

from __future__ import annotations

import dataclasses

import pytest

from satay.api.decorators import task, workflow
from satay.api.primitives import start
from satay.journal.store import SQLiteStore
from satay.testing.faults import FaultInjector, SimulatedCrash

# ---------------------------------------------------------------------------
# Fixtures of the domain: the shapes an author naturally annotates a task with.
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class Row:
    key: str


@dataclasses.dataclass
class Extracted:
    source_id: str
    rows: int


@dataclasses.dataclass
class Failure:
    reason: str


@dataclasses.dataclass
class Batch:
    rows: list[Row]
    head: Row | None = None


_EXEC: dict[str, int] = {}


def _ran(name: str) -> None:
    _EXEC[name] = _EXEC.get(name, 0) + 1


@pytest.fixture(autouse=True)
def _reset_marker() -> None:
    _EXEC.clear()


def _shape(value: object) -> str:
    """A type-identity fingerprint of ``value``, recursing into containers.

    ``Extracted(...)`` fingerprints as ``"Extracted"``; the buggy plain-dict
    degradation fingerprints as ``"dict{rows:int,source_id:str}"``, so a failure names
    exactly what went wrong.
    """
    if value is None or isinstance(value, bool):
        return type(value).__name__
    if isinstance(value, list | tuple):
        inner = ",".join(_shape(item) for item in value)
        return f"{type(value).__name__}[{inner}]"
    if isinstance(value, dict):
        inner = ",".join(f"{k}:{_shape(v)}" for k, v in sorted(value.items()))
        return f"dict{{{inner}}}"
    return type(value).__name__


# ---------------------------------------------------------------------------
# One task + one workflow per annotation shape. The single task means the crash
# armed on "TaskCompleted" always lands after that task's own completion, so the
# resumed attempt is guaranteed to take the rehydrate path.
# ---------------------------------------------------------------------------


@task()
async def rt_union_optional(n: int) -> Extracted | None:
    _ran("rt_union_optional")
    return Extracted(source_id="s3", rows=n) if n else None


@workflow
async def rt_wf_union_optional(n: int) -> str:
    return _shape(await rt_union_optional(n))


@task()
async def rt_union_two_arms(n: int) -> Extracted | Failure:
    _ran("rt_union_two_arms")
    return Extracted(source_id="s3", rows=n) if n >= 0 else Failure(reason="negative")


@workflow
async def rt_wf_union_two_arms(n: int) -> str:
    return _shape(await rt_union_two_arms(n))


@task()
async def rt_list(n: int) -> list[Row]:
    _ran("rt_list")
    return [Row(key=str(i)) for i in range(n)]


@workflow
async def rt_wf_list(n: int) -> str:
    return _shape(await rt_list(n))


@task()
async def rt_dict(n: int) -> dict[str, Batch]:
    _ran("rt_dict")
    return {"a": Batch(rows=[Row(key=str(n))], head=Row(key="h"))}


@workflow
async def rt_wf_dict(n: int) -> str:
    return _shape(await rt_dict(n))


@task()
async def rt_list_of_dict(n: int) -> list[dict[str, Row]]:
    _ran("rt_list_of_dict")
    return [{"k": Row(key=str(n))}]


@workflow
async def rt_wf_list_of_dict(n: int) -> str:
    return _shape(await rt_list_of_dict(n))


@task()
async def rt_dict_of_list(n: int) -> dict[str, list[Row]]:
    _ran("rt_dict_of_list")
    return {"k": [Row(key=str(n))]}


@workflow
async def rt_wf_dict_of_list(n: int) -> str:
    return _shape(await rt_dict_of_list(n))


@task()
async def rt_optional_in_field(n: int) -> Batch:
    _ran("rt_optional_in_field")
    return Batch(rows=[Row(key=str(n))], head=Row(key="head"))


@workflow
async def rt_wf_optional_in_field(n: int) -> str:
    batch = await rt_optional_in_field(n)
    return f"{_shape(batch)}/{_shape(batch.head)}"


@task()
async def rt_optional_union_of_containers(n: int) -> list[Row] | None:
    _ran("rt_optional_union_of_containers")
    return [Row(key=str(n))] if n else None


@workflow
async def rt_wf_optional_union_of_containers(n: int) -> str:
    return _shape(await rt_optional_union_of_containers(n))


@task()
async def rt_hetero_tuple(n: int) -> tuple[Row, int]:
    _ran("rt_hetero_tuple")
    return (Row(key=str(n)), n)


@workflow
async def rt_wf_hetero_tuple(n: int) -> str:
    return _shape(await rt_hetero_tuple(n))


CASES = [
    ("rt_wf_union_optional", rt_wf_union_optional, 2, "rt_union_optional", "Extracted"),
    ("rt_wf_union_optional_none", rt_wf_union_optional, 0, "rt_union_optional", "NoneType"),
    ("rt_wf_union_two_arms", rt_wf_union_two_arms, 1, "rt_union_two_arms", "Extracted"),
    ("rt_wf_union_two_arms_other", rt_wf_union_two_arms, -1, "rt_union_two_arms", "Failure"),
    ("rt_wf_list", rt_wf_list, 2, "rt_list", "list[Row,Row]"),
    ("rt_wf_dict", rt_wf_dict, 1, "rt_dict", "dict{a:Batch}"),
    ("rt_wf_list_of_dict", rt_wf_list_of_dict, 1, "rt_list_of_dict", "list[dict{k:Row}]"),
    ("rt_wf_dict_of_list", rt_wf_dict_of_list, 1, "rt_dict_of_list", "dict{k:list[Row]}"),
    ("rt_wf_optional_in_field", rt_wf_optional_in_field, 1, "rt_optional_in_field", "Batch/Row"),
    (
        "rt_wf_optional_union_of_containers",
        rt_wf_optional_union_of_containers,
        1,
        "rt_optional_union_of_containers",
        "list[Row]",
    ),
    ("rt_wf_hetero_tuple", rt_wf_hetero_tuple, 3, "rt_hetero_tuple", "tuple[Row,int]"),
]


@pytest.mark.parametrize(
    ("wf", "arg", "task_name", "expected"),
    [case[1:] for case in CASES],
    ids=[case[0] for case in CASES],
)
async def test_resumed_result_has_the_same_type_as_first_execution(
    wf: object, arg: int, task_name: str, expected: str
) -> None:
    store = SQLiteStore.open(":memory:")
    try:
        # 1. Clean run: the value is the task's plain Python return value.
        first = await start(wf, arg, store=store).result()
        assert _EXEC[task_name] == 1

        # 2. Crash right after the task's TaskCompleted is committed.
        injector = FaultInjector()
        injector.crash_after("TaskCompleted")
        crashed = start(wf, arg, store=store, injector=injector)
        with pytest.raises(SimulatedCrash):
            await crashed.result()
        assert _EXEC[task_name] == 2

        # 3. Resume: the recorded result is reused, so the value is *rehydrated*.
        resumed = await start(wf, arg, run_id=crashed.run_id, store=store).result()
        assert _EXEC[task_name] == 2  # reused, not re-executed
        assert await store.get_run(crashed.run_id) is not None
    finally:
        store.close()

    assert first == expected  # sanity: the shape fingerprint is what we think it is
    assert resumed == first  # the actual guard: no type drift across replay
