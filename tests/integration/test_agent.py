"""Integration: the ``satay.agent`` loop is durable because its calls are (ADR-0043).

The loop itself is runtime-agnostic; what these tests pin is the durable behaviour the
on-ramp promises when the model and tools are real ``@satay.task`` s driven inside a
``@satay.workflow``: the loop's model turns and tool calls land on the journal, a crash
mid-loop resumes without re-running recorded work, and the finished agent run is an ordinary
run that ``replay_eval`` can fork. Observable outcomes only (ADR-0011): result, status,
journal identities, an out-of-band execution counter — never replay internals.
"""

from __future__ import annotations

from typing import Any

import pytest

import satay
from satay.agent import AgentStep, ToolCall, ToolInvocation, agent_loop
from satay.journal.store import SQLiteStore
from satay.testing.faults import FaultInjector, SimulatedCrash

#: Every task body that actually ran, across every drive. A recorded call replayed as a hit
#: does not append here — which is how the crash-resume test proves reuse.
EXECUTED: list[str] = []


@pytest.fixture(autouse=True)
def _reset_executed() -> None:
    EXECUTED.clear()


def _results(transcript: tuple[Any, ...]) -> dict[str, Any]:
    return {i.call.name: i.result for i in transcript if isinstance(i, ToolInvocation)}


@satay.task()
async def _model(question: str, transcript: tuple[Any, ...], /) -> AgentStep:
    EXECUTED.append(f"model:{len(transcript)}")
    seen = _results(transcript)
    if "double" not in seen:
        return AgentStep(
            text="use the tool",
            tool_calls=(ToolCall(id="1", name="double", arguments={"n": int(question)}),),
        )
    return AgentStep(text=f"result is {seen['double']}")


@satay.task()
async def _double(call: ToolCall, /) -> int:
    EXECUTED.append("double")
    return int(call.arguments["n"]) * 2


@satay.workflow
async def _agent(question: str) -> dict[str, Any]:
    result = await agent_loop(model=_model, tools={"double": _double}, prompt=question, max_steps=5)
    return {"answer": result.output, "steps": result.steps, "stop_reason": result.stop_reason}


@pytest.fixture
def store() -> Any:
    opened = SQLiteStore.open(":memory:")
    try:
        yield opened
    finally:
        opened.close()


async def test_loop_records_its_turns_and_tool_calls_as_durable_calls(store: SQLiteStore) -> None:
    """The agent answers, and every model turn and tool call is on the journal."""
    handle = satay.start(_agent, "21", store=store)
    result: dict[str, Any] = await handle.result()

    assert result == {"answer": "result is 42", "steps": 2, "stop_reason": "final"}

    inspection = await satay.inspect(handle.run_id, store=store)
    assert [call.identity for call in inspection.calls] == [
        "_model:0",
        "_double:0",
        "_model:1",
    ]


async def test_crash_mid_loop_resumes_without_rerunning_recorded_calls(
    store: SQLiteStore,
) -> None:
    """A crash after the first turn is recorded resumes and re-runs nothing already done."""
    injector = FaultInjector()
    injector.crash_after("TaskCompleted")  # die the instant the first turn is recorded

    handle = satay.start(_agent, "21", store=store, injector=injector)
    with pytest.raises(SimulatedCrash):
        await handle.result()
    executed_before = list(EXECUTED)
    assert executed_before == ["model:0"], executed_before

    resumed = satay.start(_agent, "21", run_id=handle.run_id, store=store)
    result: dict[str, Any] = await resumed.result()

    assert result["answer"] == "result is 42"
    assert result["stop_reason"] == "final"
    # The first model turn was recorded before the crash; the resume must replay it as a hit
    # rather than run its body again. So it appears exactly once across both drives.
    assert EXECUTED.count("model:0") == 1
    # And the run's journal is the same three durable calls, regardless of the crash.
    inspection = await satay.inspect(resumed.run_id, store=store)
    assert [call.identity for call in inspection.calls] == ["_model:0", "_double:0", "_model:1"]


async def test_a_finished_agent_run_can_be_replay_evaluated(store: SQLiteStore) -> None:
    """The on-ramp feeds the wedge: an agent run is an ordinary run ``replay_eval`` forks."""
    baseline = satay.start(_agent, "21", store=store)
    assert (await baseline.result())["answer"] == "result is 42"

    # Fork before the first model turn so the new input reaches the whole loop, then diff.
    report = await satay.replay_eval(
        baseline.run_id, before_task="_model", store=store, workflow_input="50"
    )
    assert report.candidate_status == "completed"
    # The agent now doubles 50, so the final answer changes from 42 to 100.
    assert report.output.changed is True
    assert report.candidate_output["answer"] == "result is 100"
