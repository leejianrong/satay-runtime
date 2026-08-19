"""Draft five candidate replies, keep the ones that came back: collect-mode fan-out.

Best-of-N is the shape of a lot of agent code. Ask the model for several candidates under
different angles, judge them, ship the winner. The interesting part is what happens when
one candidate dies, because the default answer is that all of them do.

    uv run python examples/best_of_n_demo.py        # throwaway temp data dir
    SATAY_DATA_DIR=.satay-demo uv run python examples/best_of_n_demo.py

**The one argument this file exists to teach:** ``return_exceptions=True`` on
``satay.map`` (ADR-0027). Without it a fan-out is fail-fast, which is the right default
for a pipeline where every item has to land and the wrong one for a bake-off where the
whole point is that you only need one good answer. With it, every item settles, a failed
slot holds a ``satay.TaskFailedError`` instead of a value, and the run carries on with
what it has.

The half people miss: **a collected failure is still recorded**. It lands on the journal
as a terminal ``TaskFailed`` event next to its ``TaskAttemptFailed`` attempts, so the
failure stays visible to retries, to Studio, to the read API and to anything costing the
run. That is what separates collect mode from the old workaround of catching the error
inside the task and returning a sentinel, which records a failure as ``TaskCompleted`` and
shows you a green run over a dead candidate. ``examples/elt_pipeline_demo.py`` section 5
runs that anti-pattern and prices it.

The three parts:

1. **strict_bake_off** — the fail-fast default. One candidate never parses, one gets
   refused, and the ``map`` raises. Three finished drafts are already on the journal and
   the run is terminal, so nothing can reach them.
2. **reply_bake_off** — the same five candidates, one argument different. Three drafts
   come back beside two ``TaskFailedError``s, the judge picks a winner, the run completes,
   and both failures are on the journal as terminal ``TaskFailed`` events.
3. **interrupted_bake_off** — collect mode across a crash. A recorded ``TaskFailed``
   replays as a *hit*: the resumed run re-raises it without touching the executor, so a
   candidate that already spent its retry budget is not paid for twice. The candidate the
   crash caught part-way through its budget is, and the ledger shows the difference.

**No network, no API key, no provider SDK.** Satay ships no model adapters on purpose
(the core has near-zero dependencies, ADR-0016), so the model sits behind a one-method
protocol whose default implementation is a deterministic fake living in this file. Point
it at a real provider with ``SATAY_DEMO_MODEL=anthropic``; the example and its tests must
never need one. The seam is duplicated from the other examples rather than shared, because
every example is downloaded as one file — see the note in ``examples/agentic_dag_demo.py``.

Nothing waits on real time. ``ManualClock`` is what the retry backoff is measured against
and ``SeededRng`` pins its jitter, so a full retry schedule resolves in microseconds;
``satay.testing.settle`` is what moves the clock through each wait a drive suspends on.

By default the runs land in a throwaway temp directory, so this file is self-contained
wherever you download it. Set ``SATAY_DATA_DIR`` (or pass a path as the first argument) to
keep the journals, then ``satay dev --app examples.best_of_n_demo --data-dir <that path>``
opens all three runs in Studio.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import sys
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import satay
from satay.config import DATA_DIR_ENV_VAR, db_path
from satay.journal.events import Event, EventType
from satay.journal.store import SQLiteStore
from satay.journal.timeline import render_timeline
from satay.testing import FaultInjector, ManualClock, SeededRng, SimulatedCrash, settle

# -- the model seam ---------------------------------------------------------------


class MalformedResponseError(ValueError):
    """The provider answered, billed us, and the answer did not parse."""


class RefusedError(RuntimeError):
    """The provider declined to answer. Billed all the same, and a retry will not help."""


class NoUsableDraftError(RuntimeError):
    """Every candidate failed. Collect mode hands back the failures; the app decides."""


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

    async def complete(
        self, prompt: str, *, label: str, attempt: int = 1, context_tokens: int = 0
    ) -> Completion:
        """Complete ``prompt``.

        ``label`` names the call site, which a real client forwards as request metadata for
        cost attribution and the fake uses to decide which candidates misbehave.
        ``context_tokens`` stands in for the retrieved policy documents this demo declares
        rather than materialises.
        """
        ...


#: USD per million tokens, roughly a mid-tier frontier model in mid-2026. The shape of the
#: ledger is the point, not the vendor.
USD_PER_MTOK_IN = 3.00
USD_PER_MTOK_OUT = 15.00


def usd(input_tokens: int, output_tokens: int) -> float:
    """Price a call at the table above."""
    return (input_tokens * USD_PER_MTOK_IN + output_tokens * USD_PER_MTOK_OUT) / 1_000_000


def tokens(text: str) -> int:
    """Roughly four characters to a token, good enough for a demo ledger."""
    return max(1, len(text) // 4)


# -- the deterministic fake -------------------------------------------------------


def _field(text: str, name: str) -> str:
    """Read a ``NAME: value`` line out of a prompt or a completion. The whole parser."""
    for line in text.splitlines():
        if line.startswith(f"{name}: "):
            return line[len(name) + 2 :].strip()
    return ""


def _score(strategy: str) -> float:
    """A stable pseudo-score in [0.50, 0.95) derived from the strategy name.

    ``hashlib`` rather than ``hash()``, which is salted per process and would make this
    file print something different on every run.
    """
    digest = hashlib.sha256(strategy.encode()).digest()
    return 0.50 + (digest[0] / 256) * 0.45


#: What each angle actually says to the customer. The fake reads this; a real model would
#: work it out from the prompt.
ANGLES = {
    "refund": "process the full refund under the 30-day policy",
    "policy": "walk through what the 30-day window covers and what it does not",
    "goodwill": "apologise, credit the shipping fee, and offer a replacement",
    "escalate": "hand the thread to a specialist who will call today",
    "legal": "restate the warranty terms verbatim",
}


@dataclass
class FakeModel:
    """A model that always says the same thing, which is what CI runs.

    Every answer is a pure function of the prompt, so this file prints byte-identical
    output on every machine. ``unparseable`` and ``refusing`` name the call sites that go
    wrong, and they go wrong on every attempt: a strategy that trips a content filter or
    confuses the parser is not a transient fault, and a retry budget spent on one is money
    with nothing to show for it.
    """

    name: str = "fake-drafter-1"
    unparseable: frozenset[str] = frozenset()
    refusing: frozenset[str] = frozenset()
    #: Every physical call, billed or not: the out-of-band meter this example checks the
    #: journal against.
    calls: list[tuple[str, int, int, int]] = field(default_factory=list)

    async def complete(
        self, prompt: str, *, label: str, attempt: int = 1, context_tokens: int = 0
    ) -> Completion:
        await asyncio.sleep(0)  # yield like a real client would, without waiting on time
        if label.startswith("judge"):
            text = self._judge(prompt)
        elif label in self.refusing:
            text = "REFUSAL: I can't help with drafting that reply."
        elif label in self.unparseable:
            # Chatty preamble, no fields: what breaks a strict parser at 3am.
            text = f"Of course! Let me think about the {_field(prompt, 'STRATEGY')} angle first."
        else:
            text = self._draft(prompt)
        completion = Completion(
            text=text,
            model=self.name,
            input_tokens=tokens(prompt) + context_tokens,
            output_tokens=tokens(text),
        )
        self.calls.append((label, attempt, completion.input_tokens, completion.output_tokens))
        return completion

    def _draft(self, prompt: str) -> str:
        strategy = _field(prompt, "STRATEGY")
        customer = _field(prompt, "CUSTOMER")
        angle = ANGLES.get(strategy, "answer the question directly")
        return (
            f"REPLY: Hi {customer}, thanks for flagging this. We will {angle}.\n"
            f"CONFIDENCE: {_score(strategy):.2f}"
        )

    def _judge(self, prompt: str) -> str:
        strategies = [
            line.removeprefix("- ").split(":")[0]
            for line in prompt.splitlines()
            if line.startswith("- ")
        ]
        scored = {strategy: _score(strategy) for strategy in strategies}
        winner = max(scored, key=lambda s: scored[s])
        scores = " ".join(f"{s}={v:.2f}" for s, v in scored.items())
        return (
            f"WINNER: {winner}\n"
            f"SCORES: {scores}\n"
            f"REASON: it answers the complaint without promising anything the policy does not."
        )


class AnthropicModel:
    """The opt-in real client. Never constructed in CI, never a package dependency."""

    name = "claude-sonnet-4-5"

    async def complete(
        self, prompt: str, *, label: str, attempt: int = 1, context_tokens: int = 0
    ) -> Completion:
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


def select_model(unparseable: frozenset[str], refusing: frozenset[str]) -> ModelClient:
    """Pick the client from the environment; the deterministic fake unless told otherwise."""
    choice = os.environ.get(MODEL_ENV_VAR, "fake").strip().lower()
    if choice in {"", "fake"}:
        return FakeModel(unparseable=unparseable, refusing=refusing)
    if choice == "anthropic":
        return AnthropicModel()
    raise SystemExit(f"{MODEL_ENV_VAR}={choice!r} is not one of: fake, anthropic")


# -- the domain -------------------------------------------------------------------


@dataclass(frozen=True)
class Ticket:
    """The complaint to answer, and the angles to try on it."""

    ticket_id: str
    customer: str
    complaint: str
    strategies: list[str]


@dataclass(frozen=True)
class Candidate:
    """One angle, and the retrieved context drafting it drags along."""

    strategy: str
    ticket_id: str
    customer: str
    complaint: str
    context_tokens: int


@dataclass(frozen=True)
class Draft:
    """One candidate reply that came back and parsed."""

    strategy: str
    text: str
    confidence: float


@dataclass(frozen=True)
class Verdict:
    """The judge's pick across the surviving drafts."""

    winner: str
    reason: str
    scores: dict[str, float]


#: How much retrieved policy each angle attaches. The refund corpus and the warranty
#: corpus are the big ones, which is exactly why they are the two that go wrong.
CONTEXT_TOKENS = {
    "refund": 19_800,
    "policy": 11_200,
    "goodwill": 6_400,
    "escalate": 4_900,
    "legal": 22_500,
}

DRAFT_PROMPT = (
    "Draft one candidate reply to a support ticket, using the attached policy sources.\n"
    "CUSTOMER: {customer}\n"
    "STRATEGY: {strategy}\n"
    "COMPLAINT: {complaint}\n"
    "Answer with exactly two lines: `REPLY: ...` and `CONFIDENCE: <0-1>`.\n"
)

JUDGE_PROMPT = (
    "Pick the best candidate reply for this ticket.\n"
    "COMPLAINT: {complaint}\n"
    "CANDIDATES:\n{candidates}\n"
    "Answer with `WINNER: <strategy>`, `SCORES: <strategy>=<0-1> ...` and `REASON: ...`.\n"
)


# -- the tasks: every model call lives in one --------------------------------------


def bill(ctx: satay.TaskContext, completion: Completion) -> None:
    """Record what the provider charged, *before* anything can reject the answer.

    That ordering is the technique. The runtime flushes recorded usage onto whichever
    event ends the attempt, ``TaskCompleted`` or ``TaskAttemptFailed``, so a call that was
    billed and then failed to parse is still priced on the journal. Report after the parse
    and you only ever record the attempts that worked.
    """
    ctx.record_model_usage(
        model=completion.model,
        input_tokens=completion.input_tokens,
        output_tokens=completion.output_tokens,
        attempt=ctx.attempt,
        usd=round(usd(completion.input_tokens, completion.output_tokens), 6),
    )


@satay.task(retries=1)
async def draft(candidate: Candidate) -> Draft:
    """Draft one candidate reply. Two attempts, both billed, then the item is done for."""
    ctx = satay.task_context()
    prompt = DRAFT_PROMPT.format(
        customer=candidate.customer,
        strategy=candidate.strategy,
        complaint=candidate.complaint,
    )
    completion = await MODEL.complete(
        prompt,
        label=f"draft:{candidate.strategy}",
        attempt=ctx.attempt,
        context_tokens=candidate.context_tokens,
    )
    bill(ctx, completion)

    if completion.text.startswith("REFUSAL:"):
        raise RefusedError(f"{candidate.strategy}: {completion.text.removeprefix('REFUSAL: ')}")
    body = _field(completion.text, "REPLY")
    score = _field(completion.text, "CONFIDENCE")
    if not body or not score:
        raise MalformedResponseError(
            f"{candidate.strategy}: no REPLY/CONFIDENCE in a {completion.output_tokens}-token reply"
        )
    return Draft(strategy=candidate.strategy, text=body, confidence=float(score))


@satay.task(retries=1)
async def judge(ticket: Ticket, drafts: list[Draft]) -> Verdict:
    """Rank whatever survived and name a winner. One call, however many drafts came back."""
    ctx = satay.task_context()
    candidates = "\n".join(f"- {d.strategy}: {d.text}" for d in drafts)
    prompt = JUDGE_PROMPT.format(complaint=ticket.complaint, candidates=candidates)
    completion = await MODEL.complete(prompt, label="judge", attempt=ctx.attempt)
    bill(ctx, completion)

    winner = _field(completion.text, "WINNER")
    raw = _field(completion.text, "SCORES")
    if not winner or not raw:
        raise MalformedResponseError("the judge returned no WINNER/SCORES")
    scores = {pair.split("=")[0]: float(pair.split("=")[1]) for pair in raw.split()}
    return Verdict(winner=winner, reason=_field(completion.text, "REASON"), scores=scores)


# -- the workflow -----------------------------------------------------------------


def candidate_key(candidate: Candidate) -> str:
    """The fan-out identity of one candidate (ADR-0002: unique, stable, non-empty)."""
    return f"c-{candidate.strategy}"


async def bake_off(ticket: Ticket, *, collect: bool) -> dict[str, Any]:
    """Fan out the candidates, judge the survivors, ship the winner.

    ``collect`` is the whole experiment: it is passed straight through to
    ``return_exceptions=`` and nothing else in this body changes between the two runs.
    """
    candidates = [
        Candidate(
            strategy=strategy,
            ticket_id=ticket.ticket_id,
            customer=ticket.customer,
            complaint=ticket.complaint,
            context_tokens=CONTEXT_TOKENS.get(strategy, 5_000),
        )
        for strategy in ticket.strategies
    ]
    # Building the candidates is pure Python in the workflow body, which is allowed
    # because it is deterministic: replay recomputes the same five, so there is nothing to
    # record. The model calls below are the durable part.
    outcomes = await satay.map(
        draft, candidates, key=candidate_key, concurrency=3, return_exceptions=collect
    )

    drafts = [outcome for outcome in outcomes if not isinstance(outcome, Exception)]
    # A collected failure is always ``satay.TaskFailedError``, never the class the task
    # raised, so the value is identical on the first pass and on every replay (ADR-0027).
    # The original class name rides along in ``error_type``.
    rejected = [
        {"key": outcome.key, "error": outcome.error_type, "why": outcome.error_message}
        for outcome in outcomes
        if isinstance(outcome, satay.TaskFailedError)
    ]
    if not drafts:
        # Collect mode does not mean the run always succeeds. It means the run gets to
        # decide, and "nothing usable came back" is still a failure.
        raise NoUsableDraftError(f"{ticket.ticket_id}: all {len(candidates)} candidates failed")

    verdict = await judge(ticket, drafts)
    shipped = next(d for d in drafts if d.strategy == verdict.winner)
    return {
        "ticket_id": ticket.ticket_id,
        "winner": verdict.winner,
        "reason": verdict.reason,
        "scores": verdict.scores,
        "reply": shipped.text,
        "considered": [d.strategy for d in drafts],
        "rejected": rejected,
    }


# Three named workflows over one body, so each run is nameable in Studio's run list and in
# this example's test.


@satay.workflow
async def strict_bake_off(ticket: Ticket) -> dict[str, Any]:
    """Fail-fast, the default: one dead candidate ends the run and strands the rest."""
    return await bake_off(ticket, collect=False)


@satay.workflow
async def reply_bake_off(ticket: Ticket) -> dict[str, Any]:
    """Collect mode: every candidate settles, the survivors are judged, the run completes."""
    return await bake_off(ticket, collect=True)


@satay.workflow
async def interrupted_bake_off(ticket: Ticket) -> dict[str, Any]:
    """Collect mode across a crash: a recorded ``TaskFailed`` replays as a hit."""
    return await bake_off(ticket, collect=True)


# -- plumbing ---------------------------------------------------------------------


TICKET = Ticket(
    ticket_id="T-4471",
    customer="Priya",
    complaint="The replacement hub arrived scratched and the box was open.",
    strategies=["refund", "policy", "goodwill", "escalate", "legal"],
)

#: ``draft:refund`` answers unparseable prose on every attempt; ``draft:legal`` is refused
#: on every attempt. Both are billed in full, both attach a big retrieved corpus, and
#: neither will ever produce a draft.
UNPARSEABLE = frozenset({"draft:refund"})
REFUSING = frozenset({"draft:legal"})


def resolve_workdir() -> tuple[Path, bool]:
    """Where these runs' journals live, and whether they outlive the process."""
    override = sys.argv[1] if len(sys.argv) > 1 else os.environ.get(DATA_DIR_ENV_VAR)
    if override:
        workdir = Path(override).expanduser().resolve()
        workdir.mkdir(parents=True, exist_ok=True)
        return workdir, True
    return Path(tempfile.mkdtemp(prefix="satay-best-of-n-")), False


def keys_of(events: Iterable[Event], event_type: EventType) -> list[str]:
    """The fan-out key of every event of one type, in journal order."""
    return [
        str(event.payload["key"])
        for event in events
        if event.type is event_type and "key" in event.payload
    ]


def attempts_per_key(events: Iterable[Event]) -> dict[str, int]:
    """How many attempts each fan-out item has recorded so far."""
    counts: dict[str, int] = {}
    for key in keys_of(events, EventType.TASK_ATTEMPT_STARTED):
        counts[key] = counts.get(key, 0) + 1
    return counts


def journal_usd(events: Iterable[Event], keys: Iterable[str] | None = None) -> float:
    """What the journal says was spent, optionally narrowed to some fan-out keys.

    Reads the slot ``ctx.record_model_usage`` writes, which the runtime flushes onto
    ``TaskCompleted`` *and* ``TaskAttemptFailed`` — so a candidate that never produced a
    draft still prices itself here.
    """
    wanted = None if keys is None else set(keys)
    total = 0.0
    for event in events:
        if wanted is not None and event.payload.get("key") not in wanted:
            continue
        for entry in event.payload.get("usage", []):
            total += float(entry.get("usd", 0.0))
    return total


def tally(events: Iterable[Event]) -> dict[str, int]:
    """Journal event counts by type, in first-seen order."""
    counts: dict[str, int] = {}
    for event in events:
        counts[event.type.value] = counts.get(event.type.value, 0) + 1
    return counts


def physical_calls(model: ModelClient, since: int = 0) -> list[tuple[str, int, int, int]]:
    """Model calls made since ``since``: the fake's meter, or nothing for a real client."""
    return model.calls[since:] if isinstance(model, FakeModel) else []


# -- part 1: the fail-fast default -------------------------------------------------


async def part_one(store: SQLiteStore, clock: ManualClock, rng: SeededRng) -> str:
    """Two dead candidates, and three finished drafts nobody can reach."""
    print("1) five candidates, fail-fast fan-out (the default)")
    handle = satay.start(strict_bake_off, TICKET, store=store, clock=clock, rng=rng)
    print(f"   run {handle.run_id}")
    try:
        await settle(handle.result, clock)
    except satay.WorkflowFailedError as exc:
        print(f"   the map raised {exc.error_type}: {exc.error_message}")

    events = list(await store.read_events(handle.run_id))
    stranded = keys_of(events, EventType.TASK_COMPLETED)
    print(f"   status {await handle.status()}")
    print(f"   drafts that finished anyway: {stranded}")
    print(f"   journal: {tally(events)}")
    print(
        f"   Read that tally again. {len(stranded)} drafts committed, at "
        f"${journal_usd(events, stranded):.4f}, and the judge never ran. The run is\n"
        "   terminal, so satay.start(run_id=…) re-raises rather than resuming and forking is\n"
        "   the only way back in. No TaskFailed anywhere: under fail-fast the run's own\n"
        "   WorkflowFailed is the terminal record, which is exactly what part 2 changes."
    )
    return handle.run_id


# -- part 2: one argument ----------------------------------------------------------


async def part_two(store: SQLiteStore, clock: ManualClock, rng: SeededRng, stranded: float) -> str:
    """The same five candidates under ``return_exceptions=True``."""
    print("\n2) the same five candidates, with return_exceptions=True (ADR-0027)")
    handle = satay.start(reply_bake_off, TICKET, store=store, clock=clock, rng=rng)
    print(f"   run {handle.run_id}")
    result: dict[str, Any] = await settle(handle.result, clock)
    events = list(await store.read_events(handle.run_id))

    print(f"   status {await handle.status()}")
    print(f"   drafts judged: {result['considered']}")
    for entry in result["rejected"]:
        print(f"   rejected {entry['key']:<12} {entry['error']}: {entry['why']}")
    scores = "  ".join(f"{s}={v:.2f}" for s, v in result["scores"].items())
    print(f"   scores {scores}")
    print(f"   winner {result['winner']} — {result['reason']}")
    print(f"     | {result['reply']}")

    failed = keys_of(events, EventType.TASK_FAILED)
    print(f"\n   journal: {tally(events)}")
    print(f"   terminal TaskFailed on: {failed}")
    for event in events:
        if event.type is EventType.TASK_FAILED:
            error = event.payload["error"]
            print(
                f"     TaskFailed  key={event.payload['key']} task={event.payload['task_name']}"
                f"  {error['type']}: {error['message']}"
            )
    print(
        "   That is the half of collect mode people miss. The failure is not swallowed, it\n"
        "   is *recorded*: one terminal TaskFailed per dead candidate, beside the\n"
        "   TaskAttemptFailed events for the attempts it burned. Retry policy, Studio, the\n"
        "   read API and any cost report still see it, and a resume treats it as settled\n"
        "   rather than as work to redo. The old workaround — catching the error inside the\n"
        "   task and returning a sentinel — records a failure as TaskCompleted and hides it\n"
        "   from all of that; examples/elt_pipeline_demo.py section 5 runs it and prices it."
    )

    dead = journal_usd(events, failed)
    survivors = keys_of(events, EventType.TASK_COMPLETED)
    print(
        f"\n   what the argument bought\n"
        f"     dead candidates  ${dead:.4f}  ({len(failed)} of 5, billed in full, twice each)\n"
        f"     usable drafts    ${journal_usd(events, survivors):.4f}  ({len(survivors)} of 5)\n"
        f"     this run         ${journal_usd(events):.4f} spent, one reply shipped\n"
        f"     part 1           ${stranded:.4f} of finished drafts, unreachable, nothing shipped\n"
        "   Both runs paid for the same two dead candidates. Only one of them got anything\n"
        "   for the other three."
    )
    return handle.run_id


# -- part 3: a recorded failure replays as a hit -----------------------------------


async def part_three(store: SQLiteStore, clock: ManualClock, rng: SeededRng) -> str:
    """Crash the instant a failure becomes durable, then resume."""
    print("\n3) collect mode across a crash — a recorded TaskFailed is a replay hit")
    injector = FaultInjector()
    injector.crash_after("TaskFailed")  # die the moment the first failure is terminal
    handle = satay.start(
        interrupted_bake_off, TICKET, store=store, clock=clock, rng=rng, injector=injector
    )
    print(f"   run {handle.run_id}")
    try:
        await settle(handle.result, clock)
    except SimulatedCrash as exc:
        print(f"   worker died: {exc}")

    before = list(await store.read_events(handle.run_id))
    settled = keys_of(before, EventType.TASK_FAILED)
    print(f"   attempts before the crash: {attempts_per_key(before)}")
    print(f"   drafts committed:          {keys_of(before, EventType.TASK_COMPLETED)}")
    print(f"   terminal TaskFailed:       {settled}")
    spent_before = journal_usd(before)

    print("\n   restart the same run")
    resumed = satay.start(
        interrupted_bake_off, TICKET, run_id=handle.run_id, store=store, clock=clock, rng=rng
    )
    result: dict[str, Any] = await settle(resumed.result, clock)
    after = list(await store.read_events(handle.run_id))
    added = {
        key: count - attempts_per_key(before).get(key, 0)
        for key, count in attempts_per_key(after).items()
        if count > attempts_per_key(before).get(key, 0)
    }
    print(f"   status {await resumed.status()} — winner {result['winner']}")
    print(f"   attempts the resume added: {added}")
    print(f"   terminal TaskFailed now:   {keys_of(after, EventType.TASK_FAILED)}")
    print(
        f"     spent before the crash  ${spent_before:.4f}\n"
        f"     spent on the resume     ${journal_usd(after) - spent_before:.4f}\n"
        f"     total                   ${journal_usd(after):.4f}"
    )
    print(
        f"   {settled} had a verdict on the journal before the crash, and the resume did not\n"
        "   touch it: a recorded TaskFailed is a replay hit, so the engine re-raised it\n"
        f"   without going near the executor. {sorted(added)} did not — one attempt recorded,\n"
        "   no verdict — so the resume picked its budget up where the crash left it, at\n"
        "   attempt 2, and paid for the rest of it. Partial-completion recovery, applied to a\n"
        "   failure instead of to a success. The three drafts that had already committed cost\n"
        "   nothing to resume either, which is the part that was true before ADR-0027."
    )
    return handle.run_id


# -- main --------------------------------------------------------------------------


async def main() -> None:
    global MODEL
    workdir, durable = resolve_workdir()
    MODEL = select_model(UNPARSEABLE, REFUSING)

    clock = ManualClock()
    rng = SeededRng(20260819)  # pins the backoff jitter, so the schedule reproduces
    store = SQLiteStore.open(db_path(workdir))

    print("Satay — best of N, and what a fan-out does when a candidate dies")
    print(f"data dir: {workdir}")
    kind = "fake, deterministic" if isinstance(MODEL, FakeModel) else "live provider"
    print(f"model:    {MODEL.name} ({kind})")
    print(f"ticket:   {TICKET.ticket_id} — {TICKET.complaint}\n")

    strict_id = await part_one(store, clock, rng)
    strict_events = list(await store.read_events(strict_id))
    stranded = journal_usd(strict_events, keys_of(strict_events, EventType.TASK_COMPLETED))

    collected_id = await part_two(store, clock, rng, stranded)
    crashed_id = await part_three(store, clock, rng)

    calls = physical_calls(MODEL)
    if calls:
        journalled = 0.0
        for run_id in (strict_id, collected_id, crashed_id):
            journalled += journal_usd(list(await store.read_events(run_id)))
        billed = sum(usd(call[2], call[3]) for call in calls)
        print(
            f"\nacross all three runs: {len(calls)} model calls, ${billed:.4f} billed by the "
            f"provider\nand ${journalled:.4f} on the journals. They agree because usage is "
            "flushed onto\nTaskAttemptFailed as well as TaskCompleted, so the attempts that "
            "produced nothing\nare priced too."
        )

    print(f"\ntimeline of the collected run ({collected_id})\n")
    print(render_timeline(list(await store.read_events(collected_id)), run_id=collected_id))
    store.close()

    if durable:
        print(f"\njournals kept in {workdir}")
        print(f"open all three runs:  satay dev --app examples.best_of_n_demo --data-dir {workdir}")
        print(f"or as text:           satay runs show {collected_id} --data-dir {workdir}")
    else:
        print(
            f"\njournals went to a temp dir ({workdir}) and are not worth keeping.\n"
            "Re-run with SATAY_DATA_DIR set to browse them in Studio."
        )


if __name__ == "__main__":
    asyncio.run(main())
