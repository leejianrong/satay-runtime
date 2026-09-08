"""A durable agent loop: reason, act, observe — and survive a crash mid-thought.

The reason-act-observe loop is the shape almost every agent has. What Satay adds is that
the loop is **durable**: each model turn and each tool call is a recorded durable call, so a
crash halfway through resumes with everything already done replayed from the journal, and a
builder pays for a step exactly once. ``satay.agent.agent_loop`` is the one blessed shape
for it (ADR-0041 §3) — a thin helper, not a framework: you bring the model and the tools as
ordinary ``@satay.task`` s, it runs the loop.

    uv run python examples/durable_agent_demo.py        # throwaway temp data dir
    SATAY_DATA_DIR=.satay-demo uv run python examples/durable_agent_demo.py

What the file demonstrates, in order:

1. **The loop.** An agent answers a question by calling two tools and then replying. Two
   model turns, two tool calls, one answer — all on the journal.
2. **Durability.** Crash the process the instant the first turn is recorded, resume, and
   watch the loop finish without re-running the work it had already done. The out-of-band
   execution counter proves the recorded turns replayed as hits.
3. **The budget.** A model that never stops asking for tools is bounded by ``max_steps``,
   so a durable loop always terminates rather than appending to its journal forever.
4. **It feeds the product.** The agent run is just a run, so ``satay.replay_eval`` forks it
   and replays the last turn under a cheaper model — the PHASE 1 wedge, pointed at an agent.

**No network, no API key, no LLM SDK.** Satay ships no model adapters on purpose (ADR-0016),
and ``satay.agent`` adds none: you map your provider's reply into an ``AgentStep`` in your
own model task. Here that task is a deterministic fake, so the loop runs identically in CI
and on your laptop. Point it at a real provider with ``SATAY_DEMO_MODEL=anthropic``; the
example and its tests must never need one.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

import satay
from satay.agent import AgentStep, ToolCall, ToolInvocation, agent_loop
from satay.config import DATA_DIR_ENV_VAR, db_path
from satay.journal.store import SQLiteStore
from satay.testing import ManualClock
from satay.testing.faults import FaultInjector, SimulatedCrash

# -- the world the agent acts in ---------------------------------------------------

#: A tiny catalog and shipping table. The tools read these; the point is the loop, not the
#: data behind the tools.
CATALOG = {"widget": 4}
SHIPPING = {"2-day": 5, "overnight": 12}

#: A model tier's price per thousand tokens. The eval bridge in part 4 swaps between them.
PRICE_PER_KTOK = {"agent-std": 0.010, "agent-mini": 0.002}

#: Every physical task body that actually ran, across every drive — the out-of-band meter
#: part 2 checks the journal's reuse against. A recorded call that replays as a hit does not
#: append here, which is the whole durability argument.
EXECUTED: list[str] = []

#: The current model tier. Reassigned before the eval fork in part 4; the tasks read it at
#: execution time, so only the re-run turn bills at the new tier.
MODEL_TIER = "agent-std"

#: A question the fake never finalises — it keeps asking for a tool. Part 3 uses it to show
#: the step budget doing its job.
NEVER_ENDS = "loop forever please"


def _results(transcript: tuple[Any, ...]) -> dict[str, Any]:
    """The tool results seen so far, keyed by tool name — the agent's working memory."""
    return {item.call.name: item.result for item in transcript if isinstance(item, ToolInvocation)}


def _decide(question: str, transcript: tuple[Any, ...]) -> AgentStep:
    """The deterministic 'model'. A real one weighs the whole transcript; this keys on it.

    Answering mode: with no tool results yet, ask for a price and a shipping quote in one
    turn; once both are back, do the arithmetic and answer. Never-ending mode: always ask
    for a tool, so the loop only stops when ``max_steps`` says so.
    """
    if question == NEVER_ENDS:
        n = len(transcript)
        return AgentStep(
            text="still thinking",
            tool_calls=(ToolCall(id=f"c{n}", name="noop", arguments={}),),
        )

    seen = _results(transcript)
    if "price" not in seen or "shipping" not in seen:
        return AgentStep(
            text="Let me look up the price and the shipping.",
            tool_calls=(
                ToolCall(id="c1", name="price", arguments={"item": "widget"}),
                ToolCall(id="c2", name="shipping", arguments={"speed": "2-day"}),
            ),
        )
    total = 3 * int(seen["price"]) + int(seen["shipping"])
    return AgentStep(text=f"Three widgets plus 2-day shipping comes to ${total}.")


# -- the model and the tools: each a durable task ----------------------------------


def _bill(ctx: satay.TaskContext, text: str) -> None:
    """Record what this turn 'cost' at the current tier, so part 4 has a bill to compare."""
    output_tokens = max(1, len(text) // 4)
    ctx.record_model_usage(
        model=MODEL_TIER,
        input_tokens=20,
        output_tokens=output_tokens,
        usd=round((20 + output_tokens) / 1000 * PRICE_PER_KTOK[MODEL_TIER], 6),
    )


@satay.task()
async def model_turn(question: str, transcript: tuple[Any, ...], /) -> AgentStep:
    """One model turn. Annotated ``-> AgentStep`` so a resumed loop rehydrates it (ADR-0005)."""
    await asyncio.sleep(0)  # yield like a real client would
    EXECUTED.append(f"model:{len(transcript)}")
    step = _decide(question, transcript)
    _bill(satay.task_context(), step.text or "")
    return step


@satay.task()
async def price(call: ToolCall, /) -> int:
    """Look up an item price. A tool is just a task the loop dispatches to by name."""
    EXECUTED.append(f"price:{call.arguments.get('item')}")
    return CATALOG[str(call.arguments["item"])]


@satay.task()
async def shipping(call: ToolCall, /) -> int:
    EXECUTED.append(f"shipping:{call.arguments.get('speed')}")
    return SHIPPING[str(call.arguments["speed"])]


@satay.task()
async def noop(call: ToolCall, /) -> str:
    """A do-nothing tool, so the never-ending model in part 3 has something to call."""
    EXECUTED.append("noop")
    return "ok"


TOOLS = {"price": price, "shipping": shipping, "noop": noop}


@satay.workflow
async def assistant(question: str) -> dict[str, Any]:
    """The whole agent: one call to the loop, with the model and tools this file defines."""
    result = await agent_loop(model=model_turn, tools=TOOLS, prompt=question, max_steps=6)
    return {"answer": result.output, "steps": result.steps, "stop_reason": result.stop_reason}


@satay.workflow
async def resilient_assistant(question: str) -> dict[str, Any]:
    """The same agent under a different name, so part 2's crash-resume run stands alone on
    the journal beside part 1's (one workflow per started run keeps each nameable)."""
    result = await agent_loop(model=model_turn, tools=TOOLS, prompt=question, max_steps=6)
    return {"answer": result.output, "steps": result.steps, "stop_reason": result.stop_reason}


@satay.workflow
async def bounded_assistant(question: str) -> dict[str, Any]:
    """Same agent, a tight budget — so the never-ending model hits the ceiling in part 3."""
    result = await agent_loop(model=model_turn, tools=TOOLS, prompt=question, max_steps=3)
    return {"answer": result.output, "steps": result.steps, "stop_reason": result.stop_reason}


QUESTION = "How much is 3 widgets plus 2-day shipping?"


# -- part 1: the loop runs ----------------------------------------------------------


async def part_one(store: SQLiteStore, clock: ManualClock) -> str:
    print("1) the loop: reason, act, observe, answer")
    print(f'   question: "{QUESTION}"')
    handle = satay.start(assistant, QUESTION, store=store, clock=clock)
    result: dict[str, Any] = await handle.result()
    inspection = await satay.inspect(handle.run_id, store=store)

    print(f"\n   run {handle.run_id} — {await handle.status()}")
    print(f'     answer:      "{result["answer"]}"')
    print(f"     model turns: {result['steps']}   stop: {result['stop_reason']}")
    print(f"     durable calls on the journal: {len(inspection.calls)}")
    print(f"       {[call.identity for call in inspection.calls]}")
    return handle.run_id


# -- part 2: crash mid-loop, resume, reuse ------------------------------------------


async def part_two(store: SQLiteStore, clock: ManualClock) -> None:
    print("\n2) durability: crash mid-loop, resume, and re-run nothing already done")
    EXECUTED.clear()
    injector = FaultInjector()
    injector.crash_after("TaskCompleted")  # die the instant the first turn is recorded

    handle = satay.start(resilient_assistant, QUESTION, store=store, clock=clock, injector=injector)
    try:
        await handle.result()
    except SimulatedCrash:
        print("   crashed after the first durable call was recorded")
    executed_before = list(EXECUTED)
    run_id = handle.run_id

    # Resume the same run id with no injector: recorded calls replay as hits.
    resumed = satay.start(resilient_assistant, QUESTION, run_id=run_id, store=store, clock=clock)
    result: dict[str, Any] = await resumed.result()
    executed_after = list(EXECUTED)

    print(f"   task bodies that ran before the crash: {executed_before}")
    print(f"   task bodies that ran across both drives: {executed_after}")
    reused = [name for name in executed_before if executed_after.count(name) == 1]
    print(
        f"   >>> resumed to {await resumed.status()} with answer "
        f'"{result["answer"]}"\n'
        f"   >>> {len(reused)} recorded call(s) replayed as a hit and were not re-run: {reused}"
    )


# -- part 3: the step budget --------------------------------------------------------


async def part_three(store: SQLiteStore, clock: ManualClock) -> None:
    print("\n3) the budget: a model that never stops is bounded, so the loop terminates")
    handle = satay.start(bounded_assistant, NEVER_ENDS, store=store, clock=clock)
    result: dict[str, Any] = await handle.result()
    print(
        f"   run {handle.run_id} — {await handle.status()}\n"
        f"     model turns: {result['steps']}   stop: {result['stop_reason']}\n"
        "   The model asked for a tool every turn; max_steps=3 ended it rather than letting "
        "it\n   append to its journal forever."
    )


# -- part 4: the agent run feeds the eval product -----------------------------------


async def part_four(store: SQLiteStore, clock: ManualClock, agent_run_id: str) -> None:
    global MODEL_TIER
    print("\n4) the on-ramp feeds the wedge: replay the last turn on a cheaper model")
    MODEL_TIER = "agent-mini"
    try:
        # Fork before the final model turn (model:4 is the second turn, after two tools).
        report = await satay.replay_eval(
            agent_run_id, before_ordinal=1, before_task="model_turn", store=store, clock=clock
        )
    finally:
        MODEL_TIER = "agent-std"

    delta = report.usage_delta.get("usd", 0.0)
    print(
        f"   satay.replay_eval(agent_run, before_task='model_turn', before_ordinal=1)\n"
        f"     output changed: {report.output.changed}\n"
        f"     usd delta on the replayed turn: {delta:+g}\n"
        "   The same fork + replay + diff from PHASE 1, now pointed at an agent loop."
    )


# -- plumbing -----------------------------------------------------------------------


def resolve_workdir() -> tuple[Path, bool]:
    override = sys.argv[1] if len(sys.argv) > 1 else os.environ.get(DATA_DIR_ENV_VAR)
    if override:
        workdir = Path(override).expanduser().resolve()
        workdir.mkdir(parents=True, exist_ok=True)
        return workdir, True
    return Path(tempfile.mkdtemp(prefix="satay-agent-")), False


async def main() -> None:
    workdir, durable = resolve_workdir()
    clock = ManualClock()
    store = SQLiteStore.open(db_path(workdir))
    offline = os.environ.get("SATAY_DEMO_MODEL", "fake").strip().lower() in {"", "fake"}

    print("Satay — a durable agent loop (satay.agent.agent_loop)")
    print(f"data dir: {workdir}")
    print(f"model:    {MODEL_TIER} ({'deterministic fake, offline' if offline else 'live'})\n")

    agent_run_id = await part_one(store, clock)
    await part_two(store, clock)
    await part_three(store, clock)
    await part_four(store, clock, agent_run_id)

    store.close()
    if durable:
        print(f"\njournals kept in {workdir}")
        print(f"open the agent run:  satay runs show {agent_run_id} --data-dir {workdir}")
    else:
        print(f"\njournals went to a temp dir ({workdir}) and are not worth keeping.")


if __name__ == "__main__":
    asyncio.run(main())
