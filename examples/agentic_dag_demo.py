"""An agentic DAG with a human approval gate: plan → fan out → gather → approve → write.

This is the shape most "AI agent" code actually wants and most frameworks make you
hand-roll: a planning call, a fan-out of research calls that each retry independently, a
**human** who says go/no-go before the expensive write-up, and the ability to re-run the
finished thing under a changed prompt without paying for the research again.

    uv run python examples/agentic_dag_demo.py        # throwaway temp data dir
    SATAY_DATA_DIR=.satay-demo uv run python examples/agentic_dag_demo.py

**The one rule this file exists to teach: the model call lives in a task, never in the
workflow body.** A workflow body is replayed from the top on every resume, so anything
nondeterministic in it — a model call, a clock read, a random draw — would produce a
different answer the second time and corrupt the replay. Push it into a ``@satay.task``
and the runtime records the result once; every later replay reuses it. That single move
is what makes a model call *fakeable*, *replayable* and *retryable* at all, and it is why
this example runs in CI with **no network and no API key**.

Which is also why the model sits behind a one-method protocol (:class:`ModelClient`) whose
default implementation is a deterministic fake. Satay ships **no model adapters** — the
core has near-zero dependencies on purpose — so the fake and the optional real client both
live in this file. Point it at a real provider with ``SATAY_DEMO_MODEL=anthropic``; the
example, and its test, must never need one.

What each part shows:

1. **vendor_dossier** — plan, fan out five research questions with ``satay.map(key=...)``,
   die mid-fan-out while a flaky call is retrying, resume, clear the approval gate, and
   synthesise. Every model call is priced, and the ledger shows what durability saved.
2. **unattended_dossier** — nobody approves. The gate times out, the run takes its
   escalation branch, and the expensive synthesis is never paid for.
3. **brittle_dossier** — one source never parses. Fan-out is fail-fast (ADR-0020), so the
   run dies and the money already spent on its siblings buys nothing.
4. **fork** — the finished dossier, re-cut under a changed prompt. The research is reused
   from the journal; only the synthesis re-runs.

Nothing waits on real time: ``ManualClock`` is the clock the retry backoff and the
approval timeout are measured against, and ``SeededRng`` pins the backoff jitter, so a
four-hour review window and a full retry schedule resolve in microseconds. Something has
to move a manual clock, and that is ``satay.testing.settle`` — it drives an awaitable and
advances the clock through every wait it suspends on. Used for the workflow drives and the
worker ticks alike, since a tick can re-drive a run straight into a backoff wait.

By default the runs land in a throwaway temp directory, so this file is self-contained
wherever you download it. Set ``SATAY_DATA_DIR`` (or pass a path as the first argument) to
keep the journals, then ``satay dev --app examples.agentic_dag_demo --data-dir <that
path>`` opens them in Studio with these workflows importable, so you can start and wake
runs from the UI too.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import statistics
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Protocol

import satay
from satay.config import DATA_DIR_ENV_VAR, db_path
from satay.control.api import ControlAPI
from satay.control.commands import CommandQueue
from satay.journal.events import Event, EventType
from satay.journal.store import SQLiteStore
from satay.journal.timeline import model_usage, render_timeline
from satay.testing import FaultInjector, ManualClock, SeededRng, SimulatedCrash, settle
from satay.timers import TimerEventWorker

# -- the model seam ---------------------------------------------------------------
#
# Satay bundles no model adapters (ADR-0016: the core stays near-zero-dependency), so the
# seam is yours to declare. This protocol is deliberately the smallest thing that supports
# a retry ledger: text in, text plus token counts out.


class MalformedResponseError(ValueError):
    """The provider answered, billed us, and the answer did not parse.

    The realistic agent failure mode, and the expensive one: a retry is a second full
    prompt at full price. It is an ordinary exception, so ``@satay.task(retries=...)``
    handles it with backoff like any other.
    """


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

        ``label`` names the call site — a real client forwards it as request metadata for
        cost attribution, and the fake uses it to pick a canned answer. ``attempt`` is the
        runtime's attempt number; a real client ignores it, and the fake uses it to
        reproduce a flaky provider exactly. ``context_tokens`` stands in for retrieved
        documents this demo does not bother to materialise (see :class:`SubQuestion`).
        """
        ...


#: USD per million tokens. Roughly a mid-tier frontier model in mid-2026 — the point is
#: the shape of the ledger, not the vendor.
USD_PER_MTOK_IN = 3.00
USD_PER_MTOK_OUT = 15.00


def usd(input_tokens: int, output_tokens: int) -> float:
    """Price a call at the table above."""
    return (input_tokens * USD_PER_MTOK_IN + output_tokens * USD_PER_MTOK_OUT) / 1_000_000


def tokens(text: str) -> int:
    """Roughly four characters to a token — good enough for a demo ledger."""
    return max(1, len(text) // 4)


# -- the deterministic fake -------------------------------------------------------
#
# KAN-482 asked whether this fake and the pricing table above should move to a shared
# ``examples/_fakemodel.py``, since a reference app would want the same seam. Answer: no,
# they stay here. Recorded so it does not get re-litigated:
#
#   * Every example is downloaded as ONE file. ``docs/RELEASING.md`` §4 and every cookbook
#     page say ``curl -fsSL -O .../examples/<file>.py`` and then run it; the release
#     procedure itself does this. A sibling import breaks that for a real user, and
#     ``tests/e2e/test_examples.py`` now fails on purpose if one appears.
#   * There is nothing to de-duplicate yet. This is the only example with a model fake —
#     ``studio_walkthrough.py`` just calls ``record_model_usage`` with literal numbers. A
#     shared module would have exactly one caller.
#   * The fake is not why this file is long. The seam plus the fake is about 200 of its
#     ~900 lines; the four narrated parts at the bottom are more. If the length is the
#     problem, split the *narrative*, not the seam.
#   * The seam being visible here is the lesson (ADR-0016: Satay ships no model adapters).
#     A reader who cannot see the fake cannot see how to swap their own client in.
#
# A future reference app that needs this should copy it and diverge, or import it from its
# own package — not reach sideways into ``examples/``.


def _field(prompt: str, name: str) -> str:
    """Read a ``NAME: value`` line out of a prompt. The fake's whole parser."""
    for line in prompt.splitlines():
        if line.startswith(f"{name}: "):
            return line[len(name) + 2 :].strip()
    return ""


def _confidence(question: str) -> float:
    """A stable pseudo-confidence in [0.55, 0.95), derived from the question text.

    ``hashlib`` rather than ``hash()`` on purpose: the builtin is salted per process, so a
    demo built on it would print something different on every run.
    """
    digest = hashlib.sha256(question.encode()).digest()
    return 0.55 + (digest[0] / 256) * 0.40


@dataclass
class FakeModel:
    """A model that always says the same thing — the default, and what CI runs.

    Every answer is a pure function of the prompt, so this file prints byte-identical
    output on every machine. ``garbled_until`` reproduces the one thing that makes agent
    retries expensive: a provider that answers, bills you, and hands back something the
    parser rejects — for the first N attempts of the named call, or forever.
    """

    name: str = "fake-scribe-1"
    garbled_until: Mapping[str, int] = field(default_factory=dict)
    #: Every physical call, successful or not — the out-of-band spend meter this example
    #: compares against what the journal managed to record.
    calls: list[tuple[str, int, int, int]] = field(default_factory=list)

    async def complete(
        self, prompt: str, *, label: str, attempt: int = 1, context_tokens: int = 0
    ) -> Completion:
        await asyncio.sleep(0)  # yield like a real client would, without waiting on time
        if label.startswith("plan"):
            text = self._plan(prompt)
        elif label.startswith("research"):
            text = self._research(prompt, garbled=attempt <= self.garbled_until.get(label, 0))
        else:
            text = self._synthesis(prompt)
        completion = Completion(
            text=text,
            model=self.name,
            input_tokens=tokens(prompt) + context_tokens,
            output_tokens=tokens(text),
        )
        self.calls.append(
            (label, attempt, completion.input_tokens, completion.output_tokens),
        )
        return completion

    def _plan(self, prompt: str) -> str:
        topics = [t for t in _field(prompt, "TOPICS").split(", ") if t]
        return "\n".join(f"{t}: what should a buyer know about {t}?" for t in topics)

    def _research(self, prompt: str, *, garbled: bool) -> str:
        question = _field(prompt, "QUESTION")
        vendor = _field(prompt, "VENDOR")
        if garbled:
            # Chatty preamble, no fields: exactly what breaks a strict parser at 3am.
            return f"Sure! Here is some background on {vendor} before I answer that."
        return (
            f"FINDING: {vendor} has a documented position on "
            f"{question.rstrip('?').removeprefix('what should a buyer know about ')}.\n"
            f"CONFIDENCE: {_confidence(question):.2f}"
        )

    def _synthesis(self, prompt: str) -> str:
        style = _field(prompt, "STYLE")
        vendor = _field(prompt, "VENDOR")
        scores = [
            float(line.split("(")[1].split(")")[0])
            for line in prompt.splitlines()
            if line.startswith("- ") and "(" in line
        ]
        verdict = "hold pending a second source" if style == "sceptical" else "proceed"
        return (
            f"{style.upper()} DOSSIER — {vendor}\n"
            f"{len(scores)} findings, mean confidence {statistics.fmean(scores):.2f}.\n"
            f"Recommendation: {verdict}."
        )


class AnthropicModel:
    """The opt-in real client. Never constructed in CI, never a package dependency.

    Enabled with ``SATAY_DEMO_MODEL=anthropic`` and an ``ANTHROPIC_API_KEY``; the SDK is
    imported inside the method so this file still imports with nothing installed. The
    example's own tests must pass without any of it — that is the whole point of the seam.
    """

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


def select_model(garbled_until: Mapping[str, int]) -> ModelClient:
    """Pick the client from the environment; the deterministic fake unless told otherwise."""
    choice = os.environ.get(MODEL_ENV_VAR, "fake").strip().lower()
    if choice in {"", "fake"}:
        return FakeModel(garbled_until=garbled_until)
    if choice == "anthropic":
        return AnthropicModel()
    raise SystemExit(f"{MODEL_ENV_VAR}={choice!r} is not one of: fake, anthropic")


# -- the domain -------------------------------------------------------------------


@dataclass(frozen=True)
class Brief:
    """What the workflow is asked to produce. Plain data, so it forks cleanly."""

    vendor: str
    topics: list[str]
    #: The correlation key the reviewer's decision is delivered on. Short and boring on
    #: purpose: a high-entropy literal next to a name like this trips secret scanners, and
    #: it is a routing key, not a credential.
    review_key: str
    #: How long the gate waits for a human before escalating.
    review_window_hours: int = 4


@dataclass(frozen=True)
class SubQuestion:
    """One unit of the fan-out."""

    slug: str
    vendor: str
    text: str
    #: Size of the retrieved context a real research step would attach. Declaring the size
    #: instead of building a 40 KB string keeps the file readable and the ledger honest.
    context_tokens: int


@dataclass(frozen=True)
class Finding:
    """One answered sub-question."""

    slug: str
    text: str
    confidence: float
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class ReviewDecision:
    """The human's go/no-go, delivered with ``satay.send_event``."""

    approved: bool
    reviewer: str
    note: str = ""


#: How much retrieved context each topic drags along. Security and litigation are the
#: expensive ones, which is exactly why they are the ones that go wrong below.
CORPUS_TOKENS = {
    "pricing": 12_000,
    "security": 18_400,
    "references": 6_200,
    "roadmap": 9_100,
    "support": 7_500,
    "litigation": 21_300,
}

#: The synthesis prompt's one tunable. Mutable at module scope so part 4 can change it
#: between the source run and its fork — a prompt is data, not schedule, so a fork under a
#: changed prompt replays cleanly even with nondeterminism detection strict (ADR-0022).
SYNTHESIS_STYLE = {"value": "balanced"}

PLAN_PROMPT = (
    "You are scoping a vendor dossier.\n"
    "VENDOR: {vendor}\n"
    "TOPICS: {topics}\n"
    "Write one probing research question per topic, as `topic: question`.\n"
)

RESEARCH_PROMPT = (
    "Research one question for a vendor dossier, using the attached sources.\n"
    "VENDOR: {vendor}\n"
    "QUESTION: {question}\n"
    "Answer with exactly two lines: `FINDING: ...` and `CONFIDENCE: <0-1>`.\n"
)

SYNTHESIS_PROMPT = "Write the dossier.\nVENDOR: {vendor}\nSTYLE: {style}\nFINDINGS:\n{findings}\n"


# -- the tasks: every model call lives in one --------------------------------------
#
# `attempt` is read from the task context, so the flaky provider fails on exactly the
# attempts it is configured to fail on, and the retry ledger below is reproducible.


#: Per-logical-task spend, keyed by ``ctx.idempotency_key`` (stable across retries,
#: distinct across invocations — that is what it is for). The runtime flushes
#: ``record_model_usage`` only onto ``TaskCompleted``, so a failed attempt's tokens would
#: otherwise never reach the journal; a task that wants honest per-attempt cost has to
#: carry them itself and re-report the lot when it finally succeeds.
ATTEMPT_SPEND: dict[str, list[tuple[int, int, int]]] = {}


def bill(ctx: satay.TaskContext, completion: Completion) -> None:
    """Remember what this attempt cost, whether or not the attempt goes on to fail."""
    ATTEMPT_SPEND.setdefault(ctx.idempotency_key, []).append(
        (ctx.attempt, completion.input_tokens, completion.output_tokens)
    )


def report_every_attempt(ctx: satay.TaskContext, model: str) -> None:
    """Record one usage entry per attempt this logical task made, not just the winner."""
    for attempt, input_tokens, output_tokens in ATTEMPT_SPEND.get(ctx.idempotency_key, []):
        ctx.record_model_usage(
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            attempt=attempt,
            usd=round(usd(input_tokens, output_tokens), 6),
        )


@satay.task(retries=1)
async def plan_questions(brief: Brief) -> list[SubQuestion]:
    """Turn the brief into sub-questions. One model call, recorded once, replayed forever."""
    ctx = satay.task_context()
    prompt = PLAN_PROMPT.format(vendor=brief.vendor, topics=", ".join(brief.topics))
    completion = await MODEL.complete(prompt, label="plan", attempt=ctx.attempt)
    bill(ctx, completion)
    report_every_attempt(ctx, completion.model)

    questions = []
    for line in completion.text.splitlines():
        slug, _, text = line.partition(": ")
        if text:
            questions.append(
                SubQuestion(
                    slug=slug,
                    vendor=brief.vendor,
                    text=text,
                    context_tokens=CORPUS_TOKENS.get(slug, 5_000),
                )
            )
    if not questions:
        raise MalformedResponseError("the planner returned no usable questions")
    return questions


@satay.task(retries=2)
async def research(question: SubQuestion) -> Finding:
    """Answer one sub-question. Retried with backoff when the answer does not parse."""
    ctx = satay.task_context()
    prompt = RESEARCH_PROMPT.format(vendor=question.vendor, question=question.text)
    completion = await MODEL.complete(
        prompt,
        label=f"research:{question.slug}",
        attempt=ctx.attempt,
        context_tokens=question.context_tokens,
    )
    bill(ctx, completion)

    body = _field(completion.text, "FINDING")
    score = _field(completion.text, "CONFIDENCE")
    if not body or not score:
        # Billed in full, and worthless. The runtime will back off and try again.
        raise MalformedResponseError(
            f"{question.slug}: no FINDING/CONFIDENCE in a {completion.output_tokens}-token reply"
        )

    report_every_attempt(ctx, completion.model)
    return Finding(
        slug=question.slug,
        text=body,
        confidence=float(score),
        input_tokens=completion.input_tokens,
        output_tokens=completion.output_tokens,
    )


@satay.task(retries=1)
async def synthesize(vendor: str, findings: list[Finding]) -> str:
    """The expensive write-up. Deliberately downstream of the human gate."""
    ctx = satay.task_context()
    bullets = "\n".join(f"- {f.slug} ({f.confidence:.2f}): {f.text}" for f in findings)
    prompt = SYNTHESIS_PROMPT.format(
        vendor=vendor, style=SYNTHESIS_STYLE["value"], findings=bullets
    )
    completion = await MODEL.complete(prompt, label="synthesis", attempt=ctx.attempt)
    bill(ctx, completion)
    report_every_attempt(ctx, completion.model)
    return completion.text


# -- the workflow -----------------------------------------------------------------


def question_key(question: SubQuestion) -> str:
    """The fan-out identity of one sub-question (ADR-0002: unique, stable, non-empty).

    Derived from the question itself, never from its position, so a resumed run matches
    the same item to the same journal entry.
    """
    return f"q-{question.slug}"


async def dossier_body(brief: Brief) -> dict[str, object]:
    """plan → fan out → gather → human gate → synthesise.

    Read this as the durable-call schedule: four kinds of durable call in order, and
    nothing else. The merge between the fan-out and the gate is pure Python living
    directly in the workflow body — that is allowed precisely *because* it is
    deterministic, so replaying it produces the same numbers every time. The moment it
    needed a clock, a random draw or a network call it would have to become a task.
    """
    questions = await plan_questions(brief)
    findings = await satay.map(research, questions, key=question_key, concurrency=3)

    # Deterministic gather: no I/O, so no journal entry, and replay recomputes it exactly.
    ranked = sorted(findings, key=lambda f: f.confidence, reverse=True)
    confidence = statistics.fmean(f.confidence for f in ranked)

    decision = await satay.wait_for_event(
        ReviewDecision,
        key=brief.review_key,
        timeout=timedelta(hours=brief.review_window_hours),
    )
    if decision is None:
        # The timeout branch. A timed-out wait resolves to None — not an error — and the
        # workflow decides. Escalating here is what stops the synthesis from being paid
        # for while nobody is watching.
        return {
            "vendor": brief.vendor,
            "status": "escalated",
            "reason": f"no reviewer within {brief.review_window_hours}h",
            "confidence": round(confidence, 3),
            "questions": len(ranked),
        }
    if not decision.approved:
        return {
            "vendor": brief.vendor,
            "status": "rejected",
            "reason": decision.note,
            "confidence": round(confidence, 3),
            "questions": len(ranked),
        }

    dossier = await synthesize(brief.vendor, ranked)
    return {
        "vendor": brief.vendor,
        "status": "published",
        "reviewer": decision.reviewer,
        "confidence": round(confidence, 3),
        "questions": len(ranked),
        "dossier": dossier,
    }


# Three named scenarios over one body. They are separate workflows rather than three runs
# of one workflow because the shared example test keys a data dir's runs by workflow name
# and expects them unique — see the note above ``fork_workdir``.


@satay.workflow
async def vendor_dossier(brief: Brief) -> dict[str, object]:
    """The happy path: a crash, a retried source, an approval, a published dossier."""
    return await dossier_body(brief)


@satay.workflow
async def unattended_dossier(brief: Brief) -> dict[str, object]:
    """Nobody reviews it. The gate times out and the run escalates instead."""
    return await dossier_body(brief)


@satay.workflow
async def brittle_dossier(brief: Brief) -> dict[str, object]:
    """One source never parses, and fail-fast takes the whole run down with it."""
    return await dossier_body(brief)


# -- plumbing ---------------------------------------------------------------------


def resolve_workdir() -> tuple[Path, bool]:
    """Where these runs' journals live, and whether they outlive the process.

    An explicit argument or ``SATAY_DATA_DIR`` means the caller wants the journals kept
    (so Studio can open them); with neither, fall back to a throwaway temp directory so
    the file stays self-contained wherever it is downloaded and run.
    """
    override = sys.argv[1] if len(sys.argv) > 1 else os.environ.get(DATA_DIR_ENV_VAR)
    if override:
        workdir = Path(override).expanduser().resolve()
        workdir.mkdir(parents=True, exist_ok=True)
        return workdir, True
    return Path(tempfile.mkdtemp(prefix="satay-agentic-")), False


def fork_workdir(workdir: Path) -> Path:
    """A second data dir, for part 4 only.

    A fork is by construction a *second run of the same workflow*, and
    ``tests/e2e/test_examples.py`` reads a data dir into a dict keyed by workflow name and
    asserts those names are unique — so a fork alongside its source in the main data dir
    fails a test this file is not allowed to edit. Keeping the fork pair in its own
    directory sidesteps that, and has the side benefit that ``satay dev --data-dir`` on it
    shows nothing but the source and the fork, side by side.
    """
    forkdir = workdir / "reprompt"
    forkdir.mkdir(parents=True, exist_ok=True)
    return forkdir


def spend(calls: list[tuple[str, int, int, int]]) -> tuple[int, int, float]:
    """Total input tokens, output tokens and dollars across a slice of the call log."""
    input_tokens = sum(call[2] for call in calls)
    output_tokens = sum(call[3] for call in calls)
    return input_tokens, output_tokens, usd(input_tokens, output_tokens)


def call_log(model: ModelClient, since: int = 0) -> list[tuple[str, int, int, int]]:
    """Physical model calls made since ``since`` — the fake's meter, or nothing for a real one."""
    return model.calls[since:] if isinstance(model, FakeModel) else []


def recorded_spend(events: list[Event]) -> tuple[int, int, float]:
    """What the *journal* believes was spent, via ``ctx.record_model_usage``."""
    entries = model_usage(events)
    input_tokens = sum(int(e.get("input_tokens", 0)) for e in entries)
    output_tokens = sum(int(e.get("output_tokens", 0)) for e in entries)
    return input_tokens, output_tokens, usd(input_tokens, output_tokens)


def completed_keys(events: list[Event]) -> list[str]:
    """The fan-out key of every item whose result is durably on the journal."""
    return [
        event.payload["key"]
        for event in events
        if event.type is EventType.TASK_COMPLETED and "key" in event.payload
    ]


def money(input_tokens: int, output_tokens: int, dollars: float) -> str:
    return f"{input_tokens:>7,} in / {output_tokens:>5,} out  ${dollars:.4f}"


# -- part 1: the full agentic run --------------------------------------------------


BRIEF = Brief(
    vendor="Northwind Logistics",
    topics=["pricing", "security", "references", "roadmap", "support"],
    review_key="review-1",
)

#: ``research:security`` answers unparseable garbage on its first two attempts, then
#: behaves. Three attempts, three full prompts, one usable answer.
FLAKY = {"research:security": 2}


async def part_one(store: SQLiteStore, clock: ManualClock, rng: SeededRng) -> str:
    """Crash mid-fan-out, resume, clear the approval gate, publish."""
    worker = TimerEventWorker(store=store, clock=clock, rng=rng)
    start_of_run = len(call_log(MODEL))

    print("1) plan → fan out 5 questions → (crash) → approval gate → synthesise")
    # Die the instant the flaky research call records its first failure — mid-fan-out, with
    # some questions committed, one part-way through its retry budget, and some not started.
    injector = FaultInjector()
    injector.crash_after("TaskAttemptFailed")
    handle = satay.start(
        vendor_dossier, BRIEF, store=store, clock=clock, rng=rng, injector=injector
    )
    print(f"   run {handle.run_id}")
    try:
        await settle(handle.result, clock)
    except SimulatedCrash as exc:
        print(f"   worker died: {exc}")

    before_crash = call_log(MODEL, start_of_run)
    committed = completed_keys(list(await store.read_events(handle.run_id)))
    print(f"   model calls made before the crash: {[c[0] for c in before_crash]}")
    print(f"   fan-out results durably committed: {committed}")

    print("\n   restart the same run — committed research is reused, the rest re-runs")
    resumed = satay.start(
        vendor_dossier, BRIEF, run_id=handle.run_id, store=store, clock=clock, rng=rng
    )
    parked = await settle(resumed.result, clock)
    print(f"   drive returned {parked}; status {await resumed.status()} (parked on the gate)")

    print("\n   a human approves it: send_event, then one worker tick delivers it")
    await satay.send_event(
        ReviewDecision(approved=True, reviewer="dana", note="cleared for the board pack"),
        key=BRIEF.review_key,
        store=store,
    )
    woken = await settle(lambda: worker.tick(), clock)
    print(f"   tick woke {woken} run(s)")
    result: dict[str, object] = await resumed.result()
    print(f"   status {await resumed.status()} — {result['status']} by {result['reviewer']}")
    for line in str(result["dossier"]).splitlines():
        print(f"     | {line}")

    # -- what it cost, and what durability saved -----------------------------------
    events = list(await store.read_events(resumed.run_id))
    log = call_log(MODEL, start_of_run)
    per_label: dict[str, int] = {}
    for label, *_ in log:
        per_label[label] = per_label.get(label, 0) + 1

    print("\n   per-question ledger")
    print(f"     {'question':<16} {'model calls':<12} outcome")
    for topic in BRIEF.topics:
        label = f"research:{topic}"
        calls = per_label.get(label, 0)
        if label in FLAKY:
            outcome = (
                f"{FLAKY[label]} unparseable answers, both billed; crashed mid-budget, "
                f"resumed at attempt {FLAKY[label] + 1}"
            )
        elif f"q-{topic}" in committed:
            outcome = "committed before the crash — reused, never re-billed"
        else:
            outcome = "not started when the worker died — ran on the resume"
        print(f"     q-{topic:<14} {calls:<12} {outcome}")

    print(f"\n     actually spent   {money(*spend(log))}")
    print(f"     on the journal   {money(*recorded_spend(events))}   (record_model_usage)")
    print(
        f"     {len(set(completed_keys(events)))} answers sit on the journal and the resume"
        " re-ran only what had not committed;\n"
        "     durable execution is a cost control before it is anything else. Two caveats:\n"
        "     the fake answers instantly, so everything that started also committed, whereas\n"
        "     a real call that returned without committing is billed AGAIN on the resume; and\n"
        "     the totals match only because each task re-reports its failed attempts from an\n"
        "     in-process ledger, which a real restart would lose."
    )
    return resumed.run_id


# -- part 2: nobody approves -------------------------------------------------------


async def part_two(store: SQLiteStore, clock: ManualClock, rng: SeededRng) -> str:
    """The timeout branch of the gate."""
    worker = TimerEventWorker(store=store, clock=clock, rng=rng)
    brief = Brief(
        vendor="Southgate Freight",
        topics=["pricing", "references"],
        review_key="review-2",
        review_window_hours=4,
    )
    print("\n2) the same gate, with nobody on the other side of it")
    handle = satay.start(unattended_dossier, brief, store=store, clock=clock, rng=rng)
    print(f"   run {handle.run_id}")
    await settle(handle.result, clock)
    print(f"   status {await handle.status()} — parked, holding no coroutine and no memory")

    clock.advance(brief.review_window_hours * 3600)
    print(
        f"   {brief.review_window_hours}h later, one tick: {await settle(worker.tick, clock)}"
        " run(s) woken"
    )
    result: dict[str, object] = await handle.result()
    print(f"   status {await handle.status()} — {result['status']}: {result['reason']}")
    print("   the wait resolved to None and the workflow chose its own branch; synthesis,")
    print("   the one call that would have cost real money, never ran.")
    return handle.run_id


# -- part 3: fail-fast fan-out -----------------------------------------------------


async def part_three(store: SQLiteStore, clock: ManualClock, rng: SeededRng) -> str:
    """One dead source, and what fail-fast costs when the siblings are model calls."""
    brief = Brief(
        vendor="Eastcape Bonded",
        topics=["pricing", "litigation", "references"],
        review_key="review-3",
    )
    start_of_run = len(call_log(MODEL))
    print("\n3) one source never parses — fan-out is fail-fast (ADR-0020)")
    handle = satay.start(brittle_dossier, brief, store=store, clock=clock, rng=rng)
    print(f"   run {handle.run_id}")
    try:
        await settle(handle.result, clock)
    except satay.WorkflowFailedError as exc:
        print(f"   run failed with {exc.error_type}: {exc.error_message}")

    events = list(await store.read_events(handle.run_id))
    log = call_log(MODEL, start_of_run)
    survived = completed_keys(events)
    dead = [c for c in log if c[0] == "research:litigation"]
    siblings = [
        c for c in log if c[0].startswith("research:") and c[0] not in {"research:litigation"}
    ]

    print(f"   research answers that did commit: {survived}")
    print(f"   attempts burned on the dead source: {len(dead)} (retries=2, all of them billed)")
    print(f"     dead source      {money(*spend(dead))}")
    print(f"     its siblings     {money(*spend(siblings))}")
    print(f"     spent, in total  {money(*spend(log))}")
    print(f"     on the journal   {money(*recorded_spend(events))}")
    print(
        f"   ${spend(dead)[2]:.4f} of that never reaches the journal: usage is flushed onto\n"
        "   TaskCompleted, so a task that never completes records no tokens. Studio shows the\n"
        "   failed attempts but not what they cost.\n"
        "   The siblings' answers do survive and a resume or fork would reuse them — but this\n"
        "   workflow cannot say 'two of three answered, write it up anyway'. There is no\n"
        "   collect mode, so the caller gets an exception rather than the partial result that,\n"
        "   for a research fan-out, is usually the one you wanted. Getting it today means the\n"
        "   task swallowing its own failure and returning a sentinel — which also gives up its\n"
        "   retries, since a task that returns is a task that succeeded."
    )
    return handle.run_id


# -- part 4: fork the finished dossier under a changed prompt ----------------------


async def part_four(forkdir: Path, clock: ManualClock, rng: SeededRng) -> tuple[str, str, Path]:
    """Re-cut a completed dossier with a different synthesis prompt, reusing the research."""
    store = SQLiteStore.open(db_path(forkdir))
    queue = CommandQueue()
    control = ControlAPI(store, queue)
    worker = TimerEventWorker(store=store, clock=clock, rng=rng, commands=queue)
    brief = Brief(
        vendor="Northwind Logistics",
        topics=["pricing", "roadmap"],
        review_key="review-4",
    )

    print("\n4) fork: re-run last week's dossier under a sharper prompt")
    print(f"   (its own data dir: {forkdir})")
    handle = satay.start(vendor_dossier, brief, store=store, clock=clock, rng=rng)
    await settle(handle.result, clock)
    await satay.send_event(
        ReviewDecision(approved=True, reviewer="ravi"), key=brief.review_key, store=store
    )
    await settle(lambda: worker.tick(), clock)
    source: dict[str, object] = await handle.result()
    before = len(call_log(MODEL))
    print(f"   source run {handle.run_id} — {source['status']}")
    print(f"     | {str(source['dossier']).splitlines()[-1]}")

    events = list(await store.read_events(handle.run_id))
    synthesis_seq = min(
        e.seq
        for e in events
        if e.type is EventType.TASK_SCHEDULED and e.payload.get("task_name") == "synthesize"
    )
    fork_point = max(e.seq for e in events if e.seq < synthesis_seq)

    # A prompt is data, not schedule. Changing it leaves the workflow's durable-call
    # sequence identical, so the fork replays cleanly under strict nondeterminism
    # detection; changing which calls the workflow makes would not.
    SYNTHESIS_STYLE["value"] = "sceptical"
    fork_id = await control.fork(handle.run_id, fork_point)
    print(f"   forked at seq {fork_point} (just before synthesize was scheduled)")
    await settle(lambda: worker.tick(), clock)

    forked: dict[str, object] = await satay.start(
        vendor_dossier, brief, run_id=fork_id, store=store, clock=clock, rng=rng
    ).result()
    fork_events = list(await store.read_events(fork_id))
    lineage = next(e for e in fork_events if e.type is EventType.RUN_FORKED)
    replayed = call_log(MODEL, before)

    print(f"   fork run {fork_id} — {forked['status']}")
    print(f"     | {str(forked['dossier']).splitlines()[-1]}")
    print(
        f"   RunForked: source={lineage.payload['source_run_id']} "
        f"fork_point_seq={lineage.payload['fork_point_seq']}"
    )
    print(f"   model calls the fork actually made: {[c[0] for c in replayed]}")
    print(f"     re-synthesis {money(*spend(replayed))} — the research was reused from the")
    print("     journal, not bought again. The source run is untouched and still says")
    print(f"     '{str(source['dossier']).splitlines()[-1]}'.")

    store.close()
    return handle.run_id, fork_id, forkdir


# -- main --------------------------------------------------------------------------


async def main() -> None:
    global MODEL
    workdir, durable = resolve_workdir()
    MODEL = select_model({**FLAKY, "research:litigation": 99})

    clock = ManualClock()
    rng = SeededRng(20260731)  # pins the backoff jitter, so the delays below reproduce
    store = SQLiteStore.open(db_path(workdir))

    print("Satay — an agentic DAG with a human approval gate")
    print(f"data dir: {workdir}")
    kind = "fake, deterministic" if isinstance(MODEL, FakeModel) else "live provider"
    print(f"model:    {MODEL.name} ({kind})\n")

    published = await part_one(store, clock, rng)
    await part_two(store, clock, rng)
    await part_three(store, clock, rng)
    forkdir = fork_workdir(workdir)
    source_id, fork_id, forkdir = await part_four(forkdir, clock, rng)

    print(f"\ntimeline of the published dossier ({published})\n")
    print(render_timeline(list(await store.read_events(published)), run_id=published))
    store.close()

    if durable:
        print(f"\njournals kept in {workdir}")
        print(
            f"open the three scenarios:  satay dev --app examples.agentic_dag_demo "
            f"--data-dir {workdir}"
        )
        print(
            f"open the fork pair:        satay dev --app examples.agentic_dag_demo "
            f"--data-dir {forkdir}"
        )
        print(f"  compare {source_id} against its fork {fork_id} in Studio")
        print(f"or as text:                satay runs show {published} --data-dir {workdir}")
    else:
        print(
            f"\njournals went to a temp dir ({workdir}) and are not worth keeping.\n"
            f"Re-run with SATAY_DATA_DIR set to browse them in Studio."
        )


if __name__ == "__main__":
    asyncio.run(main())
