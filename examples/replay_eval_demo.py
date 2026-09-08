"""Replay-based evaluation: take a real run, replay it against a change, gate on the diff.

This is the wedge ADR-0041 makes the product. Every AI-observability tool reconstructs a
run from *logged traces*; Satay **re-executes** a recorded run against a changed prompt,
model, or code version and shows exactly what diverged — in output and in cost. This file
is that loop, end to end, on the public surface: ``satay.replay_eval`` forks a baseline and
drives it under the change, ``satay.gate`` turns the result into a CI pass/fail.

    uv run python examples/replay_eval_demo.py        # throwaway temp data dir
    SATAY_DATA_DIR=.satay-demo uv run python examples/replay_eval_demo.py

What the file demonstrates, in order:

1. **The baseline.** A five-call triage workflow — classify, research three topics, draft
   the reply — records what each model call cost via ``ctx.record_model_usage`` (ADR-0008).
2. **Change the prompt.** ``satay.replay_eval(run, before_task="draft", workflow_input=...)``
   forks before the draft, re-runs *only* that call under a sharper instruction, and reports
   a structured **output diff** (``.reply`` changed) and a **cost delta**. The intended
   change altered the answer: ``gate(expect_output="changed")`` passes.
3. **Change the model, cheaper.** Same input, but the draft step swaps to a mini model.
   The reply is byte-identical and the bill drops — ``gate(max_usage_increase={"usd": 0})``
   passes because cost did not rise.
4. **Change the model, pricier — the regression CI catches.** The draft step swaps to a
   top-tier model. Output unchanged, but ``usd`` climbs, and the same cost gate **fails**
   with a non-zero exit. This is the check you put in front of a merge.

The CI entry point, once these runs are on a journal, is the read-only CLI::

    satay eval <baseline_run_id> <candidate_run_id> --max-cost-increase 0 --cost-key usd

which prints the same diff and exits non-zero on a regression — no workflows imported,
because both runs already exist.

**No network, no API key, no LLM SDK.** Satay ships no model adapters on purpose (the core
has near-zero dependencies, ADR-0016), so the model sits behind a one-method protocol whose
default implementation is a deterministic fake living in this file. Every answer is a pure
function of the prompt and every price a pure function of the model, which is why this runs
identically in CI and on your laptop. Point it at a real provider with
``SATAY_DEMO_MODEL=anthropic``; the example and its tests must never need one. The seam is
duplicated from the other examples rather than shared, because every example is downloaded
as one file.

**Why the model call lives in a task.** A workflow body is replayed from the top on every
resume and every fork, so anything nondeterministic in it would answer differently the
second time. Push it into a ``@satay.task`` and the runtime records the result once — which
is exactly what makes a fork cheap and an eval honest: the reused prefix is recorded fact.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol

import satay
from satay.config import DATA_DIR_ENV_VAR, db_path
from satay.journal.store import SQLiteStore
from satay.testing import ManualClock

# -- the model seam ---------------------------------------------------------------


@dataclass(frozen=True)
class Completion:
    """One model response, with what it cost."""

    text: str
    model: str
    input_tokens: int
    output_tokens: int


class ModelClient(Protocol):
    """The whole model seam. Anything with this shape drops in."""

    name: str

    async def complete(self, prompt: str, *, label: str) -> Completion:
        """Complete ``prompt``. ``label`` names the call site; the fake keys on it."""
        ...


#: USD per million tokens, per model tier. The point is the shape of the bill and how it
#: moves when you switch model, not the vendor or the exact number.
PRICES: dict[str, tuple[float, float]] = {
    "triage-mini": (0.15, 0.60),
    "triage-std": (3.00, 15.00),
    "triage-max": (15.00, 75.00),
}


def usd(model: str, input_tokens: int, output_tokens: int) -> float:
    """Price a call at the table above."""
    per_in, per_out = PRICES[model]
    return (input_tokens * per_in + output_tokens * per_out) / 1_000_000


def tokens(text: str) -> int:
    """Roughly four characters to a token — good enough for a demo bill."""
    return max(1, len(text) // 4)


def _field(text: str, name: str) -> str:
    """Read a ``NAME: value`` line out of a prompt. The fake's parser."""
    for line in text.splitlines():
        if line.startswith(f"{name}: "):
            return line[len(name) + 2 :].strip()
    return ""


@dataclass
class FakeModel:
    """A model that always says the same thing for a given prompt — the default, and CI.

    The **text** depends only on the prompt, never on the model name, so switching model
    tiers (parts 3 and 4) moves the *bill* while leaving the *answer* byte-identical. That
    is what lets those parts isolate the cost axis. The one place text changes is the draft
    step keying on a phrase in the sharpened instruction (``be specific``) — a simulation
    of prompt sensitivity, and the honest way to make "the prompt changed the answer"
    reproducible offline. Everything downstream — the journal, the fork, the replay, the
    diff — neither knows nor cares that the model is fake.
    """

    name: str = "triage-std"
    #: Every physical call this process actually made — the out-of-band meter the tests
    #: check the journal against.
    calls: list[tuple[str, str]] = field(default_factory=list)

    async def complete(self, prompt: str, *, label: str) -> Completion:
        await asyncio.sleep(0)  # yield like a real client would, without waiting on time
        if label.startswith("classify"):
            text = self._classify(prompt)
        elif label.startswith("research"):
            text = self._research(prompt)
        else:
            text = self._draft(prompt)
        self.calls.append((label, self.name))
        MODEL_CALL_LOG.append((label, self.name))  # survives the tier swaps in use_tier
        return Completion(
            text=text,
            model=self.name,
            input_tokens=tokens(prompt),
            output_tokens=tokens(text),
        )

    def _classify(self, prompt: str) -> str:
        question = _field(prompt, "QUESTION").lower()
        category = "billing" if "charge" in question or "refund" in question else "general"
        return f"CATEGORY: {category}"

    def _research(self, prompt: str) -> str:
        topic = _field(prompt, "TOPIC")
        return f"NOTE: policy for {topic.replace('-', ' ')} is documented"

    def _draft(self, prompt: str) -> str:
        customer = _field(prompt, "CUSTOMER")
        category = _field(prompt, "CATEGORY")
        instruction = _field(prompt, "INSTRUCTION").lower()
        if "be specific" in instruction:
            return (
                f"Hi {customer}, this is a {category} question and here is exactly what "
                f"happens next, with the relevant policy cited so you can check it yourself."
            )
        return f"Hi {customer}, thanks for reaching out. We will look into this for you."


class AnthropicModel:
    """The opt-in real client. Never constructed in CI, never a package dependency."""

    name = "claude-sonnet-4-5"

    async def complete(self, prompt: str, *, label: str) -> Completion:
        from anthropic import AsyncAnthropic  # imported here, so CI never needs it

        client = AsyncAnthropic()
        message = await client.messages.create(
            model=self.name,
            max_tokens=1024,
            metadata={"user_id": label},
            messages=[{"role": "user", "content": prompt}],
        )
        return Completion(
            text="".join(block.text for block in message.content if block.type == "text"),
            model=self.name,
            input_tokens=message.usage.input_tokens,
            output_tokens=message.usage.output_tokens,
        )


#: Every physical model call this process made, across every tier swap. A module-level
#: log rather than an instance field because ``use_tier`` replaces the client, and the
#: closing meter counts calls over the whole process, not just the last tier.
MODEL_CALL_LOG: list[tuple[str, str]] = []

#: Which client the tasks call, and at which tier. Reassigned in ``main`` and, for the
#: model-swap parts, immediately before the fork that should run under the new tier.
MODEL: ModelClient = FakeModel()

#: Set to ``anthropic`` to talk to a real provider. Unset (the default) is the fake.
MODEL_ENV_VAR = "SATAY_DEMO_MODEL"


def select_model(tier: str = "triage-std") -> ModelClient:
    """Pick the client from the environment; the deterministic fake unless told otherwise."""
    choice = os.environ.get(MODEL_ENV_VAR, "fake").strip().lower()
    if choice in {"", "fake"}:
        return FakeModel(name=tier)
    if choice == "anthropic":
        return AnthropicModel()
    raise SystemExit(f"{MODEL_ENV_VAR}={choice!r} is not one of: fake, anthropic")


def use_tier(tier: str) -> None:
    """Swap the fake model to a new price tier (a no-op against a live provider).

    This is how the demo "changes the model": the tasks read :data:`MODEL` at execution
    time, so re-pointing it before a fork means only the calls the fork re-runs bill at the
    new tier. Against a real provider there is one client, so the swap is skipped.
    """
    global MODEL
    if isinstance(MODEL, FakeModel):
        MODEL = FakeModel(name=tier)


# -- the domain -------------------------------------------------------------------


@dataclass(frozen=True)
class Ticket:
    """What the agent is asked to handle. Plain data — which is what makes it forkable.

    ``instruction`` lives *in the workflow input*, which is why a fork can sharpen the
    prompt without touching the code (ADR-0028)."""

    ref: str
    customer: str
    question: str
    topics: list[str]
    instruction: str


CLASSIFY_PROMPT = "Classify this ticket.\nQUESTION: {question}\nAnswer `CATEGORY: <one word>`.\n"
RESEARCH_PROMPT = "Retrieve one policy note.\nTOPIC: {topic}\nAnswer `NOTE: ...`.\n"
DRAFT_PROMPT = (
    "Draft the customer reply.\n"
    "CUSTOMER: {customer}\n"
    "CATEGORY: {category}\n"
    "INSTRUCTION: {instruction}\n"
    "NOTES:\n{notes}\n"
)


def bill(ctx: satay.TaskContext, completion: Completion) -> None:
    """Record what the provider charged, onto the journal, at the moment of the charge."""
    ctx.record_model_usage(
        model=completion.model,
        input_tokens=completion.input_tokens,
        output_tokens=completion.output_tokens,
        usd=round(usd(completion.model, completion.input_tokens, completion.output_tokens), 6),
    )


@satay.task()
async def classify(ticket: Ticket) -> str:
    """Pick a category. One recorded model call; reused by every fork before the draft."""
    ctx = satay.task_context()
    completion = await MODEL.complete(
        CLASSIFY_PROMPT.format(question=ticket.question), label="classify"
    )
    bill(ctx, completion)
    return _field(completion.text, "CATEGORY") or "general"


@satay.task()
async def research(topic: str) -> str:
    """Retrieve one policy note. The fan-out step; also reused across draft-point forks."""
    ctx = satay.task_context()
    prompt = RESEARCH_PROMPT.format(topic=topic)
    completion = await MODEL.complete(prompt, label=f"research:{topic}")
    bill(ctx, completion)
    return _field(completion.text, "NOTE")


@satay.task()
async def draft(ticket: Ticket, category: str, notes: list[str]) -> str:
    """Write the reply. **This is the call the evals re-run.**"""
    ctx = satay.task_context()
    prompt = DRAFT_PROMPT.format(
        customer=ticket.customer,
        category=category,
        instruction=ticket.instruction,
        notes="\n".join(f"- {note}" for note in notes),
    )
    completion = await MODEL.complete(prompt, label="draft")
    bill(ctx, completion)
    return completion.text


@satay.workflow
async def handle_ticket(ticket: Ticket) -> dict[str, Any]:
    """classify → research each topic → draft the reply.

    Five durable calls: one classify, three research, one draft. The output is plain data,
    so an eval can diff it field by field."""
    category = await classify(ticket)
    notes = await satay.map(research, ticket.topics, key=lambda topic: topic, concurrency=3)
    reply = await draft(ticket, category, list(notes))
    return {"ref": ticket.ref, "category": category, "reply": reply}


# -- the ticket, and the two instructions -------------------------------------------

TERSE_INSTRUCTION = "Answer the customer."
SHARPER_INSTRUCTION = "Answer the customer, name the category, and be specific about next steps."

TICKET = Ticket(
    ref="TCK-4471",
    customer="Sam",
    question="I was charged twice for one order. Can you help?",
    topics=["duplicate-charge", "refund-window", "proof-of-purchase"],
    instruction=TERSE_INSTRUCTION,
)


# -- rendering ----------------------------------------------------------------------


def show_delta(report: satay.EvalReport) -> None:
    """Print the output verdict and the per-key cost delta from one report."""
    if report.output.changed:
        print(f"     output:      changed at {', '.join(report.output.paths) or '.'}")
    elif report.output.redacted:
        print("     output:      equality unknown (redacted in the journal)")
    else:
        print("     output:      unchanged")
    changed = [c.identity for c in report.calls.changed]
    print(f"     calls diff:  {len(changed)} of {len(report.calls.calls)} changed  {changed}")
    for key in ("usd", "input_tokens", "output_tokens"):
        if key in report.usage_delta:
            base = report.baseline_usage.get(key, 0)
            cand = report.candidate_usage.get(key, 0)
            print(f"     {key:<12} {base}  ->  {cand}   (delta {report.usage_delta[key]:+g})")


def show_gate(label: str, result: satay.GateResult) -> None:
    """Print a gate verdict and, on failure, why."""
    print(f"     gate [{label}]: {'PASS' if result.passed else 'FAIL'}")
    for reason in result.reasons:
        print(f"       - {reason}")


# -- part 1: the baseline -----------------------------------------------------------


async def part_one(store: SQLiteStore, clock: ManualClock) -> str:
    """Run the ticket once under the terse instruction and record what it cost."""
    print("1) the baseline run")
    print(f'   ticket {TICKET.ref} — "{TICKET.question}"')
    print(f'   instruction: "{TICKET.instruction}"')

    handle = satay.start(handle_ticket, TICKET, store=store, clock=clock)
    result: dict[str, Any] = await handle.result()
    base = await satay.inspect(handle.run_id, store=store)

    print(f"\n   run {handle.run_id} — {await handle.status()}")
    print(f"     category: {result['category']}")
    print(f'     reply:    "{result["reply"]}"')
    print(
        f"     {len(base.calls)} durable calls   "
        f"{base.usage.get('input_tokens', 0)} in / {base.usage.get('output_tokens', 0)} out   "
        f"${base.usage.get('usd', 0.0):.4f}"
    )
    return handle.run_id


# -- part 2: change the prompt ------------------------------------------------------


async def part_two(store: SQLiteStore, clock: ManualClock, baseline_id: str) -> str:
    """Sharpen the instruction, replay only the draft, and gate on the intended change."""
    print("\n2) change the prompt: replay under a sharper instruction")
    sharper = replace(TICKET, instruction=SHARPER_INSTRUCTION)
    print('     satay.replay_eval(run, before_task="draft", workflow_input=sharper)')

    report = await satay.replay_eval(
        baseline_id,
        before_task="draft",
        workflow_input=sharper,
        store=store,
        clock=clock,
    )
    candidate = await satay.inspect(report.candidate_run_id, store=store)
    print(f"     candidate {report.candidate_run_id} — {report.candidate_status}")
    print(f'     new reply: "{candidate.output["reply"]}"')
    show_delta(report)
    # An eval expects the change to *do* something, so a changed output is the pass.
    show_gate("expect_output=changed", satay.gate(report, expect_output="changed"))
    # The same report read as a regression test (must match baseline) would fail — same
    # data, opposite question. Both are one call to `gate`, which is the point.
    show_gate("expect_output=unchanged", satay.gate(report, expect_output="unchanged"))
    return report.candidate_run_id


# -- part 3: change the model, cheaper ----------------------------------------------


async def part_three(store: SQLiteStore, clock: ManualClock, baseline_id: str) -> str:
    """Swap the draft step to a mini model, same input; the bill drops, the answer holds."""
    print("\n3) change the model: replay the draft on a cheaper tier, same input")
    use_tier("triage-mini")
    print('     use mini model; satay.replay_eval(run, before_task="draft")  # input inherited')

    report = await satay.replay_eval(baseline_id, before_task="draft", store=store, clock=clock)
    show_delta(report)
    # Cost must not rise; output is not gated here (a model swap is allowed to change it).
    show_gate(
        "max usd increase 0",
        satay.gate(report, expect_output="any", max_usage_increase={"usd": 0.0}),
    )
    use_tier("triage-std")  # restore, so part 4 measures against the same baseline tier
    return report.candidate_run_id


# -- part 4: change the model, pricier — the regression CI catches ------------------


async def part_four(store: SQLiteStore, clock: ManualClock, baseline_id: str) -> str:
    """Swap the draft step to a top-tier model; the cost gate fails and the process would too."""
    print("\n4) change the model: replay the draft on a pricier tier — the regression")
    use_tier("triage-max")
    print('     use max model; satay.replay_eval(run, before_task="draft")  # input inherited')

    report = await satay.replay_eval(baseline_id, before_task="draft", store=store, clock=clock)
    show_delta(report)
    result = satay.gate(report, expect_output="any", max_usage_increase={"usd": 0.0})
    show_gate("max usd increase 0", result)
    print(
        "\n   In CI this is the failure that blocks the merge. The same check, with both\n"
        "   runs already on a journal, is:\n"
        f"     satay eval {baseline_id} {report.candidate_run_id} "
        "--max-cost-increase 0 --cost-key usd\n"
        "   which prints this diff and exits non-zero."
    )
    use_tier("triage-std")
    return report.candidate_run_id


# -- plumbing -----------------------------------------------------------------------


def resolve_workdir() -> tuple[Path, bool]:
    """Where these runs' journals live, and whether they outlive the process."""
    override = sys.argv[1] if len(sys.argv) > 1 else os.environ.get(DATA_DIR_ENV_VAR)
    if override:
        workdir = Path(override).expanduser().resolve()
        workdir.mkdir(parents=True, exist_ok=True)
        return workdir, True
    return Path(tempfile.mkdtemp(prefix="satay-eval-")), False


async def main() -> None:
    global MODEL
    workdir, durable = resolve_workdir()
    MODEL = select_model("triage-std")
    clock = ManualClock()
    store = SQLiteStore.open(db_path(workdir))

    print("Satay — replay-based evaluation: fork a real run, replay the change, gate the diff")
    print(f"data dir: {workdir}")
    kind = "deterministic fake, offline" if isinstance(MODEL, FakeModel) else "live provider"
    print(f"model:    {MODEL.name} ({kind})\n")

    baseline_id = await part_one(store, clock)
    await part_two(store, clock, baseline_id)
    await part_three(store, clock, baseline_id)
    await part_four(store, clock, baseline_id)

    if isinstance(MODEL, FakeModel):
        runs = len(await store.list_runs())
        # Each eval reused the four-call prefix and re-ran only the draft, so the process
        # made far fewer model calls than runs * 5 — the arithmetic that makes eval cheap.
        print(
            f"\n{runs} runs of a five-call workflow, and this process made "
            f"{len(MODEL_CALL_LOG)} model calls, not {runs * 5}."
        )
    store.close()

    if durable:
        print(f"\njournals kept in {workdir}")
        print(f"gate a candidate against the baseline:  satay eval {baseline_id} <candidate>")
    else:
        print(
            f"\njournals went to a temp dir ({workdir}) and are not worth keeping.\n"
            "Re-run with SATAY_DATA_DIR set to gate them with `satay eval`."
        )


if __name__ == "__main__":
    asyncio.run(main())
