"""``satay.agent`` — one durable agent loop, the on-ramp (ADR-0043, implementing ADR-0041 §3).

The reason-act-observe loop is the shape every agent consumer hand-rolls: ask a model,
run the tools it asks for, feed the results back, repeat until it answers. ADR-0025 §4 said
teach it in examples and *promote it only once users have hand-rolled it enough*; ADR-0041
§3 makes the call — ship **one** blessed loop so a builder lands in ten minutes, and hold
the line at exactly that. This is that loop and nothing more.

**It is not a framework, and it is deletable.** The strongest evidence is that this module
imports nothing from ``satay`` at all: the loop is pure control flow over awaitables the
caller supplies. What makes it *durable* is entirely the caller's doing — the ``model`` and
``tools`` are ordinary ``@satay.task`` s, and the loop runs inside a ``@satay.workflow``.
So a crash mid-loop resumes with every recorded model turn and tool result replayed as a
journal hit, and a builder pays for a step exactly once. Delete this file and the runtime
is untouched (the ADR-0032 test, applied to the loop). It stays a *submodule* rather than
joining the five top-level primitives, because ADR-0041 §3 calls it a watched on-ramp, not
a sixth primitive.

**What it deliberately is not** (ADR-0025 §4, reaffirmed by ADR-0041 §3): no provider
adapter — you map your provider's response into :class:`AgentStep` in your own model task;
no tool registry or dispatch framework — ``tools`` is a plain ``{name: task}`` mapping you
own; no graph, no planner, no second loop variant. A second shape is a new decision, not a
licence to grow this one.

Typical use::

    @satay.task()
    async def model(prompt: str, transcript: tuple, /) -> satay.agent.AgentStep:
        # call your provider, then map its reply into an AgentStep
        ...

    @satay.task()
    async def search(call: satay.agent.ToolCall, /) -> str:
        return do_search(call.arguments["query"])

    @satay.workflow
    async def assistant(question: str) -> str | None:
        result = await satay.agent.agent_loop(
            model=model, tools={"search": search}, prompt=question
        )
        return result.output

Because a replayed run rehydrates a task's output against its **return annotation**
(ADR-0005), the model task must be annotated ``-> AgentStep`` for the loop to read
``turn.tool_calls`` after a resume. Nothing here reaches for a clock, randomness, or I/O, so
the loop body is replay-safe: its control flow turns only on values the journal recorded.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

#: Default ceiling on model turns. A durable loop must terminate — an agent that never stops
#: asking for tools would append to its journal forever — so the loop always has a bound, and
#: this is it when the caller names none.
MAX_STEPS_DEFAULT = 12


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One tool invocation the model asked for, with arguments already parsed.

    Neutral by design: you build these in your model task from whatever your provider
    returns, so no part of Satay knows a provider's tool-call wire format."""

    id: str
    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AgentStep:
    """One model turn: the assistant's text and any tool calls it requested.

    A turn with no tool calls is *final* — the model answered rather than reaching for a
    tool — and ends the loop. Your model task returns one of these; the loop never parses a
    provider response itself."""

    text: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()

    @property
    def is_final(self) -> bool:
        """True when the model asked for no tools, i.e. this turn is the answer."""
        return not self.tool_calls


@dataclass(frozen=True, slots=True)
class ToolInvocation:
    """A tool call paired with what the tool returned — one observation in the transcript."""

    call: ToolCall
    result: Any


@dataclass(frozen=True, slots=True)
class AgentResult:
    """What :func:`agent_loop` returns: the answer, how it stopped, and the full transcript."""

    output: str | None
    """The final model turn's text — the answer on ``stop_reason == "final"``, or the last
    thing the model said before the step budget ran out."""

    steps: int
    """How many model turns ran (each an ``await model(...)``)."""

    stop_reason: str
    """``"final"`` — the model answered without asking for a tool — or ``"max_steps"`` — the
    budget was exhausted first. A string, not a bool, so a third reason later is additive."""

    transcript: tuple[AgentStep | ToolInvocation, ...] = ()
    """The turns and tool observations in the order they happened. The record the debugger
    wedge (fork, replay, diff) reads back."""


class UnknownToolError(RuntimeError):
    """The model asked for a tool name the ``tools`` mapping does not contain.

    Raised rather than fed back to the model: silently swallowing it would be the first step
    toward a retry/repair policy, which is the loop-framework surface ADR-0041 §3 refuses.
    Catch it in your workflow if you want to recover; the loop's job is to be honest about
    what happened, not to decide the recovery."""

    def __init__(self, name: str) -> None:
        super().__init__(f"the model asked for an unknown tool: {name!r}")
        self.tool_name = name


#: A model turn: given the prompt and the transcript so far, return the next
#: :class:`AgentStep`. In durable use this is a ``@satay.task`` annotated ``-> AgentStep``.
ModelStep = Callable[[Any, tuple[AgentStep | ToolInvocation, ...]], Awaitable[AgentStep]]

#: A tool: given the :class:`ToolCall`, return its result. In durable use, a ``@satay.task``.
ToolFn = Callable[[ToolCall], Awaitable[Any]]


async def agent_loop(
    *,
    model: ModelStep,
    tools: Mapping[str, ToolFn],
    prompt: Any,
    max_steps: int = MAX_STEPS_DEFAULT,
) -> AgentResult:
    """Run the reason-act-observe loop until the model answers or the step budget runs out.

    Each turn calls ``model(prompt, transcript)`` for an :class:`AgentStep`. If the step is
    final (no tool calls) the loop returns it. Otherwise every requested :class:`ToolCall` is
    dispatched to ``tools[call.name]`` in order, each result appended to the transcript, and
    the loop asks the model again. The transcript is a tuple of :class:`AgentStep` and
    :class:`ToolInvocation` in the order they occurred; it is what each model turn sees and
    what the returned :class:`AgentResult` carries.

    Durability is inherited, not built here: make ``model`` and each tool a ``@satay.task``
    and call this inside a ``@satay.workflow``, and a crash resumes with every prior turn and
    tool result replayed from the journal rather than re-executed. The loop body itself is
    deterministic — its branches turn only on recorded task outputs — so replay is exact.

    ``max_steps`` bounds the model turns (default :data:`MAX_STEPS_DEFAULT`); reaching it
    returns ``stop_reason == "max_steps"`` with the last text seen. Raises
    :class:`UnknownToolError` if the model names a tool absent from ``tools``, and
    :class:`ValueError` if ``max_steps`` is below 1.
    """
    if max_steps < 1:
        raise ValueError(f"max_steps must be at least 1, not {max_steps}")

    transcript: list[AgentStep | ToolInvocation] = []
    steps = 0
    last_text: str | None = None

    while steps < max_steps:
        turn = await model(prompt, tuple(transcript))
        steps += 1
        transcript.append(turn)
        last_text = turn.text
        if turn.is_final:
            return AgentResult(
                output=turn.text,
                steps=steps,
                stop_reason="final",
                transcript=tuple(transcript),
            )
        for call in turn.tool_calls:
            tool = tools.get(call.name)
            if tool is None:
                raise UnknownToolError(call.name)
            result = await tool(call)
            transcript.append(ToolInvocation(call=call, result=result))

    return AgentResult(
        output=last_text,
        steps=steps,
        stop_reason="max_steps",
        transcript=tuple(transcript),
    )


__all__ = [
    "MAX_STEPS_DEFAULT",
    "AgentResult",
    "AgentStep",
    "ModelStep",
    "ToolCall",
    "ToolFn",
    "ToolInvocation",
    "UnknownToolError",
    "agent_loop",
]
