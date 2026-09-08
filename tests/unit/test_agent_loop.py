"""Unit tests for the ``satay.agent`` loop logic (ADR-0043).

``satay.agent.agent_loop`` imports nothing from the runtime — it is pure control flow over
awaitables the caller supplies — so its logic is tested here with plain ``async def`` fakes,
no store and no workflow drive. The *durable* behaviour (recording, crash-and-resume) is a
property of the caller's tasks and lives in ``tests/integration/test_agent.py``.
"""

from __future__ import annotations

from typing import Any

import pytest

from satay.agent import (
    AgentStep,
    ToolCall,
    ToolInvocation,
    UnknownToolError,
    agent_loop,
)


def _tool_results(transcript: tuple[Any, ...]) -> dict[str, Any]:
    return {i.call.name: i.result for i in transcript if isinstance(i, ToolInvocation)}


async def test_final_turn_ends_the_loop_without_calling_a_tool() -> None:
    """A turn with no tool calls is the answer: one model turn, no tools, stop 'final'."""

    async def model(prompt: Any, transcript: tuple[Any, ...]) -> AgentStep:
        return AgentStep(text=f"answer to {prompt}")

    result = await agent_loop(model=model, tools={}, prompt="q")

    assert result.output == "answer to q"
    assert result.steps == 1
    assert result.stop_reason == "final"
    assert len(result.transcript) == 1
    assert isinstance(result.transcript[0], AgentStep)


async def test_dispatches_tools_and_feeds_results_back() -> None:
    """The model asks for a tool, the loop runs it, and the next turn sees its result."""
    calls: list[ToolCall] = []

    async def echo(call: ToolCall) -> str:
        calls.append(call)
        return f"echoed:{call.arguments['value']}"

    async def model(prompt: Any, transcript: tuple[Any, ...]) -> AgentStep:
        seen = _tool_results(transcript)
        if "echo" not in seen:
            return AgentStep(
                text="let me use a tool",
                tool_calls=(ToolCall(id="1", name="echo", arguments={"value": prompt}),),
            )
        return AgentStep(text=f"final: {seen['echo']}")

    result = await agent_loop(model=model, tools={"echo": echo}, prompt="hi")

    assert result.output == "final: echoed:hi"
    assert result.steps == 2
    assert result.stop_reason == "final"
    # The tool was handed the ToolCall, once.
    assert [c.name for c in calls] == ["echo"]
    # Transcript is turn, observation, turn — in that order.
    assert [type(item).__name__ for item in result.transcript] == [
        "AgentStep",
        "ToolInvocation",
        "AgentStep",
    ]
    observation = result.transcript[1]
    assert isinstance(observation, ToolInvocation)
    assert observation.result == "echoed:hi"


async def test_unknown_tool_raises_naming_the_tool() -> None:
    """A tool the mapping does not contain is a loud error, not a silent no-op."""

    async def model(prompt: Any, transcript: tuple[Any, ...]) -> AgentStep:
        return AgentStep(tool_calls=(ToolCall(id="1", name="missing", arguments={}),))

    with pytest.raises(UnknownToolError) as excinfo:
        await agent_loop(model=model, tools={}, prompt="q")
    assert excinfo.value.tool_name == "missing"


async def test_max_steps_bounds_a_model_that_never_finalises() -> None:
    """A model that asks for a tool every turn is stopped by the budget, not left running."""

    async def noop(call: ToolCall) -> str:
        return "ok"

    async def model(prompt: Any, transcript: tuple[Any, ...]) -> AgentStep:
        return AgentStep(text="again", tool_calls=(ToolCall(id="x", name="noop"),))

    result = await agent_loop(model=model, tools={"noop": noop}, prompt="q", max_steps=3)

    assert result.stop_reason == "max_steps"
    assert result.steps == 3
    assert result.output == "again"  # the last text seen before the budget ran out


async def test_max_steps_must_be_positive() -> None:
    """A non-positive budget is a programming error, caught before the loop runs."""

    async def model(prompt: Any, transcript: tuple[Any, ...]) -> AgentStep:  # pragma: no cover
        return AgentStep(text="unreached")

    with pytest.raises(ValueError):
        await agent_loop(model=model, tools={}, prompt="q", max_steps=0)


def test_agent_step_is_final_reads_tool_calls() -> None:
    """``is_final`` is exactly 'the model asked for no tools'."""
    assert AgentStep(text="done").is_final is True
    assert AgentStep(tool_calls=(ToolCall(id="1", name="t"),)).is_final is False
