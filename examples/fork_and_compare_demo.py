"""The debugger loop: a run goes wrong, fork it before the bad call, compare the two.

Nothing here crashes. A support agent answers a customer, the answer is confidently
wrong, and the run finishes ``completed`` — the failure mode no stack trace catches and
no retry fixes, because the code did exactly what it was told.

    uv run python examples/fork_and_compare_demo.py        # throwaway temp data dir
    SATAY_DATA_DIR=.satay-demo uv run python examples/fork_and_compare_demo.py

What the file demonstrates, in order:

1. **The bad run.** Six durable calls — plan, four policy lookups, one draft — under an
   instruction that tells the model to keep the customer happy. It promises a refund the
   policy does not allow and cites a policy id that does not exist.
2. **The fork.** ``satay.fork(run_id, before_task="draft_reply", workflow_input=...)``
   cuts the journal immediately before the bad call and re-runs it under a sharper
   instruction. One call executes; the other five answer from the copied prefix.
3. **The bill.** The two numbers side by side, because that is the argument.
4. **The compare.** ``ReadAPI.compare`` aligns both runs by durable-call identity, so
   "five identical, one differs" is read off two real journals rather than asserted.
5. **The rule.** A fork's copied prefix is *history*, so ``workflow_input=`` reaches only
   the calls after the fork point. The demo forks twice more to show what that costs when
   you get it wrong and what it looks like when you get it right (ADR-0028).

**No network, no API key, no LLM SDK.** Satay ships no model adapters on purpose (the
core has near-zero dependencies, ADR-0016), so the model sits behind a one-method
protocol whose default implementation is a deterministic fake living in this file. Every
answer is a pure function of the prompt, which is why this runs identically in CI and on
your laptop. Point it at a real provider with ``SATAY_DEMO_MODEL=anthropic``; the example
and its tests must never need one.

**Why the model call lives in a task.** A workflow body is replayed from the top on every
resume and on every fork, so anything nondeterministic in it would answer differently the
second time. Push it into a ``@satay.task`` and the runtime records the result once. That
single move is what makes a fork cheap: the recorded answers are reusable facts.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import tempfile
import textwrap
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol

import satay
from satay.config import DATA_DIR_ENV_VAR, db_path
from satay.control.api import ReadAPI
from satay.control.views import call_identity
from satay.journal.events import Event, EventType
from satay.journal.store import SQLiteStore
from satay.journal.timeline import model_usage
from satay.testing import ManualClock

# -- the model seam ---------------------------------------------------------------


class MalformedResponseError(ValueError):
    """The provider answered, billed us, and the answer did not parse."""


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

    async def complete(self, prompt: str, *, label: str, context_tokens: int = 0) -> Completion:
        """Complete ``prompt``.

        ``label`` names the call site — a real client forwards it as request metadata for
        cost attribution, and the fake uses it to pick a branch. ``context_tokens`` stands
        in for the retrieved documents this demo declares rather than materialises.
        """
        ...


#: USD per million tokens. Roughly a mid-tier frontier model in mid-2026 — the point is
#: the shape of the bill, not the vendor.
USD_PER_MTOK_IN = 3.00
USD_PER_MTOK_OUT = 15.00


def usd(input_tokens: int, output_tokens: int) -> float:
    """Price a call at the table above."""
    return (input_tokens * USD_PER_MTOK_IN + output_tokens * USD_PER_MTOK_OUT) / 1_000_000


def tokens(text: str) -> int:
    """Roughly four characters to a token — good enough for a demo bill."""
    return max(1, len(text) // 4)


def _field(text: str, name: str) -> str:
    """Read a ``NAME: value`` line out of a prompt or a completion. The fake's parser."""
    for line in text.splitlines():
        if line.startswith(f"{name}: "):
            return line[len(name) + 2 :].strip()
    return ""


@dataclass
class FakeModel:
    """A model that always says the same thing — the default, and what CI runs.

    Every answer is a pure function of the prompt, so this file prints byte-identical
    output on every machine.

    The draft branch is the one that matters. A real model reads the whole instruction and
    weighs it; a deterministic fake cannot, so this one keys on a single phrase from the
    sharpened instruction (``only from the notes``). That is a simulation of prompt
    sensitivity, not a model of it — and it is the honest way to make "the prompt was the
    bug" reproducible offline. Everything downstream of the answer is real: the journal,
    the fork, the replay and the comparison do not know or care that the model is fake.
    """

    name: str = "fake-support-1"
    #: Every physical call — the out-of-band meter this example checks the journal against.
    calls: list[tuple[str, int, int]] = field(default_factory=list)

    async def complete(self, prompt: str, *, label: str, context_tokens: int = 0) -> Completion:
        await asyncio.sleep(0)  # yield like a real client would, without waiting on time
        if label.startswith("plan"):
            text = self._plan(prompt)
        elif label.startswith("lookup"):
            text = self._lookup(prompt)
        else:
            text = self._draft(prompt)
        completion = Completion(
            text=text,
            model=self.name,
            input_tokens=tokens(prompt) + context_tokens,
            output_tokens=tokens(text),
        )
        self.calls.append((label, completion.input_tokens, completion.output_tokens))
        return completion

    def _plan(self, prompt: str) -> str:
        topics = [t for t in _field(prompt, "TOPICS").split(", ") if t]
        return "\n".join(f"{t}: which policy covers {t.replace('-', ' ')}?" for t in topics)

    def _lookup(self, prompt: str) -> str:
        return f"NOTE: {_field(prompt, 'SOURCE')}\nPOLICY: {_field(prompt, 'POLICY_ID')}"

    def _draft(self, prompt: str) -> str:
        customer = _field(prompt, "CUSTOMER")
        instruction = _field(prompt, "INSTRUCTION")
        days = int(_field(prompt, "DAYS_SINCE_DELIVERY") or 0)
        window = int(_field(prompt, "REFUND_WINDOW_DAYS") or 0)
        notes = [line[2:] for line in prompt.splitlines() if line.startswith("- ")]

        if "only from the notes" not in instruction.lower():
            # Warm, confident, and inventing both the outcome and the policy it cites.
            # POL-09 is not in the library below; that is the whole point of it.
            return (
                f"Hi {customer} — so sorry about this! We have refunded you in full, no "
                f"questions asked, and a replacement is already on its way. The details "
                f"are all in our returns policy POL-09."
            )

        grounded = " ".join(f"{note}." for note in notes)
        if "POL-22" not in grounded:  # the refund-window policy is not in the notes
            closing = "Tell me which option suits you and I will set it up."
        elif days <= window:
            closing = "I can issue a refund."
        else:
            closing = (
                f"Your order was delivered {days} days ago, so the {window}-day window "
                f"has closed and I cannot issue a refund; I can offer store credit or a "
                f"like-for-like replacement instead."
            )
        return f"Hi {customer} — thanks for the details. {grounded} {closing}"


class AnthropicModel:
    """The opt-in real client. Never constructed in CI, never a package dependency."""

    name = "claude-sonnet-4-5"

    async def complete(self, prompt: str, *, label: str, context_tokens: int = 0) -> Completion:
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


#: Which client the tasks call. Swapped once, in ``main``, before anything runs.
MODEL: ModelClient = FakeModel()

#: Set to ``anthropic`` to talk to a real provider. Unset (the default) is the fake.
MODEL_ENV_VAR = "SATAY_DEMO_MODEL"


def select_model() -> ModelClient:
    """Pick the client from the environment; the deterministic fake unless told otherwise."""
    choice = os.environ.get(MODEL_ENV_VAR, "fake").strip().lower()
    if choice in {"", "fake"}:
        return FakeModel()
    if choice == "anthropic":
        return AnthropicModel()
    raise SystemExit(f"{MODEL_ENV_VAR}={choice!r} is not one of: fake, anthropic")


# -- the domain -------------------------------------------------------------------


#: The retrieval corpus: topic → (policy id, the retrieved sentence, its context size).
#: Declaring the size instead of building a 70 KB string keeps the file readable and the
#: bill honest — the lookups are expensive precisely because they drag documents along.
POLICY_LIBRARY: dict[str, tuple[str, str, int]] = {
    "damaged-on-arrival": (
        "POL-14",
        "Items damaged in transit are covered for 30 days from delivery [POL-14]",
        18_400,
    ),
    "refund-window": (
        "POL-22",
        "Refunds are issued inside 30 days of delivery, and store credit after that [POL-22]",
        21_300,
    ),
    "proof-of-purchase": (
        "POL-31",
        "An order number is proof enough and no receipt photo is required [POL-31]",
        6_200,
    ),
    "replacement-vs-refund": (
        "POL-40",
        "A like-for-like replacement may be offered before a refund [POL-40]",
        9_100,
    ),
    "address-change": (
        "POL-51",
        "A delivery address can be changed until the carrier scans the parcel [POL-51]",
        14_700,
    ),
    "in-transit-redirect": (
        "POL-58",
        "Once scanned, only the carrier can redirect a parcel and it may refuse [POL-58]",
        16_800,
    ),
    "carrier-handoff": (
        "POL-63",
        "After handoff the label cannot be altered and the order returns to sender [POL-63]",
        12_500,
    ),
    "reshipment-fee": (
        "POL-70",
        "A reshipment after a failed delivery carries a flat handling fee [POL-70]",
        7_500,
    ),
}

#: How long after delivery a refund is still allowed. The business rule the guardrail
#: checks the draft against.
REFUND_WINDOW_DAYS = 30

#: A promise of money back, in the shapes this fake produces. A real guardrail is a
#: bigger regex or a second model; the shape of the check is the same either way.
REFUND_PROMISE = re.compile(r"refunded you in full|full refund|money back", re.IGNORECASE)

#: Every ``POL-nn`` the draft claims to be quoting.
CITATION = re.compile(r"POL-\d+")


@dataclass(frozen=True)
class Brief:
    """What the agent is asked to do. Plain data — which is what makes it forkable."""

    ticket: str
    customer: str
    question: str
    topics: list[str]
    days_since_delivery: int
    #: The prompt. It lives *in the workflow input*, which is the whole reason a fork can
    #: change it without touching the code (ADR-0028).
    instruction: str


@dataclass(frozen=True)
class Lookup:
    """One policy the planner decided to look up."""

    topic: str
    text: str


@dataclass(frozen=True)
class Note:
    """One retrieved policy, summarised."""

    topic: str
    policy_id: str
    text: str


PLAN_PROMPT = (
    "You are triaging a customer support ticket.\n"
    "QUESTION: {question}\n"
    "TOPICS: {topics}\n"
    "Write one policy lookup per topic, as `topic: lookup`.\n"
)

LOOKUP_PROMPT = (
    "Summarise one retrieved policy for a support agent.\n"
    "TOPIC: {topic}\n"
    "POLICY_ID: {policy_id}\n"
    "SOURCE: {source}\n"
    "Answer with exactly two lines: `NOTE: ...` and `POLICY: POL-nn`.\n"
)

DRAFT_PROMPT = (
    "Draft the reply to the customer.\n"
    "CUSTOMER: {customer}\n"
    "QUESTION: {question}\n"
    "DAYS_SINCE_DELIVERY: {days}\n"
    "REFUND_WINDOW_DAYS: {window}\n"
    "INSTRUCTION: {instruction}\n"
    "NOTES:\n{notes}\n"
)


def bill(ctx: satay.TaskContext, completion: Completion) -> None:
    """Record what the provider charged, onto the journal, at the moment of the charge."""
    ctx.record_model_usage(
        model=completion.model,
        input_tokens=completion.input_tokens,
        output_tokens=completion.output_tokens,
        usd=round(usd(completion.input_tokens, completion.output_tokens), 6),
    )


# -- the tasks: every model call lives in one --------------------------------------


@satay.task()
async def plan_lookups(brief: Brief) -> list[Lookup]:
    """Turn the ticket into policy lookups. One model call, recorded once."""
    ctx = satay.task_context()
    prompt = PLAN_PROMPT.format(question=brief.question, topics=", ".join(brief.topics))
    completion = await MODEL.complete(prompt, label="plan")
    bill(ctx, completion)

    lookups = [
        Lookup(topic=topic, text=text)
        for topic, _, text in (line.partition(": ") for line in completion.text.splitlines())
        if text and topic in POLICY_LIBRARY
    ]
    if not lookups:
        raise MalformedResponseError("the planner returned no usable lookups")
    return lookups


@satay.task()
async def look_up(lookup: Lookup) -> Note:
    """Retrieve one policy and summarise it. The expensive half of the run."""
    ctx = satay.task_context()
    policy_id, source, context_tokens = POLICY_LIBRARY[lookup.topic]
    prompt = LOOKUP_PROMPT.format(topic=lookup.topic, policy_id=policy_id, source=source)
    completion = await MODEL.complete(
        prompt, label=f"lookup:{lookup.topic}", context_tokens=context_tokens
    )
    bill(ctx, completion)

    text = _field(completion.text, "NOTE")
    cited = _field(completion.text, "POLICY")
    if not text or not cited:
        raise MalformedResponseError(f"{lookup.topic}: the summary did not parse")
    return Note(topic=lookup.topic, policy_id=cited, text=text)


@satay.task()
async def draft_reply(brief: Brief, notes: list[Note]) -> str:
    """Write the customer-facing reply. **This is the call the fork re-runs.**"""
    ctx = satay.task_context()
    prompt = DRAFT_PROMPT.format(
        customer=brief.customer,
        question=brief.question,
        days=brief.days_since_delivery,
        window=REFUND_WINDOW_DAYS,
        instruction=brief.instruction,
        notes="\n".join(f"- {note.text}" for note in notes),
    )
    completion = await MODEL.complete(prompt, label="draft")
    bill(ctx, completion)
    return completion.text


# -- the workflow -------------------------------------------------------------------


def lookup_key(lookup: Lookup) -> str:
    """The fan-out identity of one lookup (ADR-0002: unique, stable, non-empty)."""
    return lookup.topic


def review(reply: str, notes: list[Note], brief: Brief) -> dict[str, object]:
    """The guardrail: does the draft cite real policies, and does it promise the possible?

    Pure Python living in the workflow body, which is allowed precisely *because* it is
    deterministic — no clock, no randomness, no I/O — so replaying it produces the same
    verdict every time and it needs no journal entry of its own.
    """
    cited = sorted(set(CITATION.findall(reply)))
    known = {note.policy_id for note in notes}
    hallucinated = [policy for policy in cited if policy not in known]
    promises_refund = REFUND_PROMISE.search(reply) is not None
    refund_allowed = brief.days_since_delivery <= REFUND_WINDOW_DAYS
    return {
        "retrieved": sorted(known),
        "cited": cited,
        "hallucinated": hallucinated,
        "promises_refund": promises_refund,
        "refund_allowed": refund_allowed,
        "ok": not hallucinated and not (promises_refund and not refund_allowed),
    }


@satay.workflow
async def answer_ticket(brief: Brief) -> dict[str, object]:
    """plan → look up four policies → draft the reply → audit it.

    Read this as the durable-call schedule: three kinds of durable call, six calls in
    total, and nothing else. The audit is ordinary Python in the body.
    """
    lookups = await plan_lookups(brief)
    notes = await satay.map(look_up, lookups, key=lookup_key, concurrency=4)
    reply = await draft_reply(brief, notes)
    return {"ticket": brief.ticket, "reply": reply, **review(reply, notes, brief)}


# -- the two tickets ----------------------------------------------------------------

#: The prompt that loses money. Nothing about it is a strawman — it is the instruction a
#: support team writes on day one, before anybody has thought about what the model will
#: do with "whatever it takes".
EAGER_INSTRUCTION = "Reassure the customer and keep them happy, whatever it takes"

#: The prompt that does not. The phrase the fake keys on is `only from the notes`.
GROUNDED_INSTRUCTION = (
    "Answer only from the notes below, quote the policy id behind every claim, "
    "and never promise an outcome the notes do not allow"
)

REFUND_TICKET = Brief(
    ticket="TCK-8814",
    customer="Dana",
    question="My blender arrived cracked. Can I get a refund?",
    topics=["damaged-on-arrival", "refund-window", "proof-of-purchase", "replacement-vs-refund"],
    days_since_delivery=35,  # past the 30-day window: a refund is not on the table
    instruction=EAGER_INSTRUCTION,
)

#: A *different question* on the same workflow, used in part 5 to show what the input
#: override does and does not reach.
ADDRESS_TICKET = Brief(
    ticket="TCK-9032",
    customer="Dana",
    question="I have moved. Can I still change the delivery address on this order?",
    topics=["address-change", "in-transit-redirect", "carrier-handoff", "reshipment-fee"],
    days_since_delivery=2,
    instruction=GROUNDED_INSTRUCTION,
)


# -- reading the journal -------------------------------------------------------------


def fork_seq(events: list[Event]) -> int:
    """The seq of this run's ``RunForked`` marker, or 0 for a run that was started.

    A fork's journal opens with a **verbatim copy** of the source's prefix, so everything
    at or below this seq is inherited history and everything above it is work this run
    actually did. That distinction is the entire measurement below.
    """
    return next((e.seq for e in events if e.type is EventType.RUN_FORKED), 0)


def executed_here(events: list[Event]) -> list[str]:
    """The durable calls whose Python body actually ran in *this* run.

    A ``TaskAttemptStarted`` above the fork marker means the executor entered your
    function. Below it, the event is a copy of one the source run wrote.
    """
    boundary = fork_seq(events)
    return [
        call_identity(event.payload)
        for event in events
        if event.seq > boundary and event.type is EventType.TASK_ATTEMPT_STARTED
    ]


def durable_calls(events: list[Event]) -> list[str]:
    """Every durable-call identity this run resolved, replayed or executed."""
    seen = []
    for event in events:
        if event.type is EventType.TASK_SCHEDULED:
            identity = call_identity(event.payload)
            if identity not in seen:
                seen.append(identity)
    return seen


def billed_here(events: list[Event]) -> tuple[int, int, float]:
    """Input tokens, output tokens and dollars this run was charged, above the fork marker.

    The copied prefix carries the source's usage entries too. They are *history*, not a
    second charge, so they are excluded here — which is exactly the arithmetic that makes
    a fork cheap.
    """
    boundary = fork_seq(events)
    entries = model_usage([e for e in events if e.seq > boundary])
    input_tokens = sum(int(e.get("input_tokens", 0)) for e in entries)
    output_tokens = sum(int(e.get("output_tokens", 0)) for e in entries)
    return input_tokens, output_tokens, usd(input_tokens, output_tokens)


def policies_used(events: list[Event]) -> list[str]:
    """The topics of the keyed ``look_up`` calls on this run's journal, copied or not."""
    return [identity.split(":key:")[1] for identity in durable_calls(events) if ":key:" in identity]


def lineage(events: list[Event]) -> dict[str, Any]:
    """The ``RunForked`` payload: where this fork came from and whether its input changed."""
    return next(e.payload for e in events if e.type is EventType.RUN_FORKED)


def money(input_tokens: int, output_tokens: int, dollars: float) -> str:
    return f"{input_tokens:>7,} in / {output_tokens:>5,} out  ${dollars:.4f}"


def quote(reply: str, indent: str = "     | ") -> str:
    """Render a customer-facing reply as a quoted block."""
    return "\n".join(indent + line for line in textwrap.wrap(reply, width=72))


def verdict(result: dict[str, Any]) -> str:
    return "PASSED" if result["ok"] else "FAILED"


# -- part 1: the run that went wrong --------------------------------------------------


async def part_one(store: SQLiteStore, clock: ManualClock) -> tuple[str, dict[str, Any]]:
    """Answer the ticket under the eager instruction, and audit what came back."""
    print("1) the run that went wrong")
    print(
        f"   ticket {REFUND_TICKET.ticket} — delivered {REFUND_TICKET.days_since_delivery} "
        f"days ago, and the refund window is {REFUND_WINDOW_DAYS} days"
    )
    print(f'     "{REFUND_TICKET.question}"')
    print(f'   instruction: "{REFUND_TICKET.instruction}"')

    handle = satay.start(answer_ticket, REFUND_TICKET, store=store, clock=clock)
    result: dict[str, Any] = await handle.result()
    events = list(await store.read_events(handle.run_id))

    print(f"\n   run {handle.run_id} — {await handle.status()}")
    print(quote(str(result["reply"])))
    print(f"\n   guardrail: {verdict(result)}")
    print(f"     policies the run retrieved  {result['retrieved']}")
    print(f"     policy ids the reply cites  {result['cited']}")
    print(f"     ids that do not exist       {result['hallucinated']}  <- invented")
    print(f"     promises money back         {result['promises_refund']}")
    print(f"     policy allows one           {result['refund_allowed']}  <- 35 days > 30")
    print(
        "\n   Nothing raised. The run is `completed` and the workflow did exactly what it\n"
        "   was told. A stack trace shows nothing and a retry produces the same answer,\n"
        "   because the bug is in the input, not in the code."
    )
    print(f"\n     {len(durable_calls(events))} durable calls   {money(*billed_here(events))}")
    return handle.run_id, result


# -- part 2: fork before the bad call -------------------------------------------------


async def part_two(
    store: SQLiteStore, clock: ManualClock, source_id: str
) -> tuple[str, dict[str, Any]]:
    """Re-cut the same run under a sharper instruction, reusing everything before it."""
    sharper = replace(REFUND_TICKET, instruction=GROUNDED_INSTRUCTION)

    print("\n2) fork it immediately before the bad call, under a sharper instruction")
    print('     satay.fork(run_id, before_task="draft_reply", workflow_input=sharper)')
    print("   instruction:")
    print(quote(sharper.instruction, indent="     > "))

    handle = await satay.fork(
        source_id, before_task="draft_reply", workflow_input=sharper, store=store, clock=clock
    )
    result: dict[str, Any] = await handle.result()
    events = list(await store.read_events(handle.run_id))
    marker = lineage(events)

    print(f"\n   fork run {handle.run_id} — {await handle.status()}")
    print(quote(str(result["reply"])))
    print(f"\n   guardrail: {verdict(result)}")
    print(f"     policies the run retrieved  {result['retrieved']}")
    print(f"     policy ids the reply cites  {result['cited']}")
    print(f"     ids that do not exist       {result['hallucinated']}")
    print(f"     promises money back         {result['promises_refund']}")
    print(
        f"   RunForked: source={marker['source_run_id']} "
        f"fork_point_seq={marker['fork_point_seq']} "
        f"input_overridden={marker.get('input_overridden', False)}"
    )
    return handle.run_id, result


# -- part 3: the bill -----------------------------------------------------------------


async def part_three(store: SQLiteStore, source_id: str, fork_id: str) -> None:
    """The number the whole feature is for."""
    source = list(await store.read_events(source_id))
    forked = list(await store.read_events(fork_id))
    total = len(durable_calls(source))
    ran = executed_here(forked)
    reused = total - len(ran)
    source_bill = billed_here(source)
    fork_bill = billed_here(forked)

    print("\n3) what the fork actually re-ran")
    print(f"   durable calls it executed           {len(ran)}  {ran}")
    print(f"   durable calls it read off the copy  {reused}")
    print(f"\n     the source run   {money(*source_bill)}")
    print(f"     the fork         {money(*fork_bill)}")
    print(
        f"\n   >>> {len(ran)} of {total} durable calls re-ran; {reused} were reused "
        f"byte-identical.\n"
        f"   >>> ${fork_bill[2]:.4f} to fix the answer, against ${source_bill[2]:.4f} for "
        f"the original run —\n"
        f"   >>> {1 - fork_bill[2] / source_bill[2]:.1%} of the bill was history, and "
        f"history does not need re-buying.\n"
        f"   >>> The source run is untouched and still says what it said."
    )


# -- part 4: compare, call by call ----------------------------------------------------


def cell(side: dict[str, Any] | None) -> str:
    return "absent" if side is None else str(side["status"])


async def part_four(store: SQLiteStore, source_id: str, fork_id: str) -> None:
    """Align the two runs by durable-call identity and read the divergence off the table."""
    view = await ReadAPI(store).compare(source_id, fork_id)
    # ``compare`` returns rows sorted by identity, which is stable but alphabetical.
    # Re-order them into the order the source run actually issued the calls, because the
    # point being made is "everything before the cut, then the one call after it".
    schedule = durable_calls(list(await store.read_events(source_id)))
    rows = sorted(view["rows"], key=lambda row: schedule.index(row["identity"]))

    print("\n4) compare, call by call")
    print(f"     ReadAPI.compare({source_id[:8]}…, {fork_id[:8]}…)")
    print(f"     GET /runs/{source_id}/compare?to={fork_id}")
    print(f"\n   {'durable call':<35}{'source':<11}{'fork':<11}recorded output")
    identical = 0
    for row in rows:
        left, right = row["a"], row["b"]
        same = left is not None and right is not None and left["output"] == right["output"]
        identical += same
        note = "identical — replayed" if same else "DIFFERS  <- the fixed call"
        print(f"   {row['identity']:<35}{cell(left):<11}{cell(right):<11}{note}")
    print(
        f"\n   {len(rows)} calls aligned on both sides; {identical} identical, "
        f"{len(rows) - identical} different — and the\n"
        "   one that differs is the one call after the fork point. Studio draws this\n"
        "   table; the JSON behind it is what you just read off two real journals."
    )


# -- part 5: the rule the loop has ----------------------------------------------------


async def part_five(store: SQLiteStore, clock: ManualClock, source_id: str) -> tuple[str, str]:
    """The copied prefix wins, so the fork point decides what the new input reaches."""
    print("\n5) the rule: a fork's copied prefix is history, not a prediction")
    print("   Same fork point, but a different QUESTION this time — an address change,")
    print("   not a refund. `draft_reply` is the only call after the cut, so it is the")
    print("   only call that sees the new ticket:")

    stale = await satay.fork(
        source_id,
        before_task="draft_reply",
        workflow_input=ADDRESS_TICKET,
        store=store,
        clock=clock,
    )
    stale_result: dict[str, Any] = await stale.result()
    stale_events = list(await store.read_events(stale.run_id))

    print(f"\n   fork run {stale.run_id} — {await stale.status()}, guardrail ", end="")
    print(f"{verdict(stale_result)}, and useless")
    print(quote(str(stale_result["reply"])))
    print(f"     durable calls it executed:  {executed_here(stale_events)}")
    print(f"     policies on its journal:    {', '.join(policies_used(stale_events))}")
    print(
        "   Every citation is real, so the guardrail passes. The research is simply\n"
        "   answering the previous question, because those four calls already happened\n"
        "   and a fork reuses them rather than paying for them again."
    )

    print("\n   Fork before `plan_lookups` instead and the new input reaches everything:")
    fresh = await satay.fork(
        source_id,
        before_task="plan_lookups",
        workflow_input=ADDRESS_TICKET,
        store=store,
        clock=clock,
    )
    fresh_result: dict[str, Any] = await fresh.result()
    fresh_events = list(await store.read_events(fresh.run_id))

    print(f"\n   fork run {fresh.run_id} — {await fresh.status()}, guardrail ", end="")
    print(f"{verdict(fresh_result)}")
    print(quote(str(fresh_result["reply"])))
    print(
        f"     durable calls it executed:  {len(executed_here(fresh_events))} of "
        f"{len(durable_calls(fresh_events))}   {money(*billed_here(fresh_events))}"
    )
    print(f"     policies on its journal:    {', '.join(policies_used(fresh_events))}")
    print(
        "\n   So: put the fork point before the first durable call that should see the new\n"
        "   input (ADR-0028). `before_task=` exists to let you say exactly that, and the\n"
        "   full-price run above is what it costs when the honest answer is 'all of them'."
    )
    return stale.run_id, fresh.run_id


# -- plumbing --------------------------------------------------------------------------


def resolve_workdir() -> tuple[Path, bool]:
    """Where these runs' journals live, and whether they outlive the process."""
    override = sys.argv[1] if len(sys.argv) > 1 else os.environ.get(DATA_DIR_ENV_VAR)
    if override:
        workdir = Path(override).expanduser().resolve()
        workdir.mkdir(parents=True, exist_ok=True)
        return workdir, True
    return Path(tempfile.mkdtemp(prefix="satay-fork-")), False


async def main() -> None:
    global MODEL
    workdir, durable = resolve_workdir()
    MODEL = select_model()
    clock = ManualClock()
    store = SQLiteStore.open(db_path(workdir))

    print("Satay — the debugger loop: fork a prefix, replay, compare call by call")
    print(f"data dir: {workdir}")
    kind = "deterministic fake, offline" if isinstance(MODEL, FakeModel) else "live provider"
    print(f"model:    {MODEL.name} ({kind})\n")

    source_id, _ = await part_one(store, clock)
    fork_id, _ = await part_two(store, clock, source_id)
    await part_three(store, source_id, fork_id)
    await part_four(store, source_id, fork_id)
    await part_five(store, clock, source_id)

    if isinstance(MODEL, FakeModel):
        runs = len(await store.list_runs())
        calls = len(MODEL.calls)
        print(
            f"\nfour runs of a six-call workflow, and this process made {calls} model calls,"
            f"\nnot {runs * 6}. The other {runs * 6 - calls} answered from the journal."
        )
    store.close()

    app = "examples.fork_and_compare_demo"
    if durable:
        print(f"\njournals kept in {workdir}")
        print(f"open all four runs:  satay dev --app {app} --data-dir {workdir}")
        print(f"  then compare {source_id} against its fork {fork_id}")
        print(f"or as text:          satay runs show {source_id} --data-dir {workdir}")
    else:
        print(
            f"\njournals went to a temp dir ({workdir}) and are not worth keeping.\n"
            "Re-run with SATAY_DATA_DIR set to open them in Studio."
        )


if __name__ == "__main__":
    asyncio.run(main())
