# An Agentic DAG

Plan a set of research questions. Fan them out, each retrying on its own. Wait for a human to
approve. Only then pay for the expensive write-up. Then re-run the finished thing under a sharper
prompt without buying the research again.

That is the shape most agent code wants and most frameworks make you hand-roll. It is also the
recipe carrying the single most important lesson in these docs, and the lesson fits in one
sentence: the model call lives in a **task**, never in the workflow body.

Source: [`examples/agentic_dag_demo.py`](https://github.com/leejianrong/satay-runtime/blob/v0.1.0a3/examples/agentic_dag_demo.py)
(919 lines, so this page excerpts it)

## Get It And Run It

```bash
pip install 'satay[studio]'
curl -fsSL -O https://raw.githubusercontent.com/leejianrong/satay-runtime/v0.1.0a3/examples/agentic_dag_demo.py
SATAY_DATA_DIR=.satay-demo python agentic_dag_demo.py
```

No API key, no network, no provider SDK. It runs against a deterministic fake model by default,
which is the point of the next section rather than a shortcut around it.

## Why The Model Call Has To Be A Task

A workflow body is **replayed from the top on every resume**. So anything nondeterministic in it,
a model call, a clock read, a random draw, would produce a different answer the second time and
corrupt the replay. The engine would find a recorded schedule that no longer matches what the code
is doing.

Push the call into a `@satay.task` and the runtime records its result once. Every later replay
hands back that recorded result without calling the provider. That single move is what makes a
model call **fakeable**, **replayable**, and **retryable** at all:

- **Fakeable**, because the task is the seam. Swap the client behind it and the workflow does not
  change, which is why this file runs in CI with no key.
- **Replayable**, because the answer is on the journal. A resume re-executes the body and gets the
  same text back, so a nine-step agent that dies on step eight does not re-plan from scratch.
- **Retryable**, because `@satay.task(retries=2)` wraps a unit the executor can attempt again. A
  garbled response raises, backs off, and tries once more, and every attempt lands on the journal.

Here is the body, and what is notable is how little is in it:

```python
async def dossier_body(brief: Brief) -> dict[str, object]:
    """plan → fan out → gather → human gate → synthesise."""
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
        return {..., "status": "escalated"}
    if not decision.approved:
        return {..., "status": "rejected"}

    dossier = await synthesize(brief.vendor, ranked)
    return {..., "status": "published", "dossier": dossier}
```

Four kinds of durable call, in order, and nothing else. The `sorted` and the `fmean` between the
fan-out and the gate are pure Python living directly in the workflow body, and that is allowed
**precisely because** they are deterministic: replaying them produces the same numbers every time,
so they need no journal entry. The moment either one needed a clock, a random draw, or a network
call it would have to become a task.

Read [the determinism rule](../determinism.md) if you want the full statement. This example is
that rule doing visible work.

## The Model Seam

Satay ships **no model adapters**. The core has near-zero dependencies on purpose, so the seam is
yours to declare, and here it is the smallest thing that supports a cost ledger:

```python
class ModelClient(Protocol):
    """The whole model seam. Anything with this shape drops in."""

    name: str

    async def complete(
        self, prompt: str, *, label: str, attempt: int = 1, context_tokens: int = 0
    ) -> Completion: ...
```

The default implementation is a fake whose every answer is a pure function of the prompt, so the
file prints the same numbers on every machine:

```python
@dataclass
class FakeModel:
    """A model that always says the same thing — the default, and what CI runs."""

    name: str = "fake-scribe-1"
    garbled_until: Mapping[str, int] = field(default_factory=dict)
    calls: list[tuple[str, int, int, int]] = field(default_factory=list)
```

`garbled_until` reproduces the failure mode that makes agent retries expensive: a provider that
answers, bills you, and hands back something the parser rejects. In this run, `research:security`
does that on its first two attempts.

The real client is opt-in and imports its SDK inside the method, so the file still imports with
nothing installed:

```python
class AnthropicModel:
    """The opt-in real client. Never constructed in CI, never a package dependency."""

    name = "claude-sonnet-4-5"

    async def complete(self, prompt, *, label, attempt=1, context_tokens=0) -> Completion:
        from anthropic import AsyncAnthropic  # imported here, so CI never needs it
        ...
```

To use it:

```bash
SATAY_DEMO_MODEL=anthropic ANTHROPIC_API_KEY=sk-fake-placeholder python agentic_dag_demo.py
```

!!! tip "The fake is not a testing compromise, it is the design working"

    You can only substitute the model because the call sits inside a task. If it were inline in
    the workflow body there would be nothing to substitute, no recorded result to replay, and no
    unit for the executor to retry. Structure your own agents this way and the deterministic fake
    falls out for free. So does the ability to replay a production run offline.

## Part 1: Crash, Resume, Approve, Publish

```console
$ SATAY_DATA_DIR=.satay-demo python agentic_dag_demo.py
Satay — an agentic DAG with a human approval gate
data dir: …/.satay-demo
model:    fake-scribe-1 (fake, deterministic)

1) plan → fan out 5 questions → (crash) → approval gate → synthesise
   run 8d75038de27b4f5a883c3ecce351760f
   worker died: simulated crash after event 'TaskAttemptFailed'
   model calls made before the crash: ['plan', 'research:pricing', 'research:security', 'research:references']
   fan-out results durably committed: ['q-pricing', 'q-references']

   restart the same run — committed research is reused, the rest re-runs
   drive returned <parked>; status waiting (parked on the gate)

   a human approves it: send_event, then one worker tick delivers it
   tick woke 1 run(s)
   status completed — published by dana
     | BALANCED DOSSIER — Northwind Logistics
     | 5 findings, mean confidence 0.77.
     | Recommendation: proceed.

   per-question ledger
     question         model calls  outcome
     q-pricing        1            committed before the crash — reused, never re-billed
     q-security       3            2 unparseable answers, both billed; crashed mid-budget, resumed at attempt 3
     q-references     1            committed before the crash — reused, never re-billed
     q-roadmap        1            not started when the worker died — ran on the resume
     q-support        1            not started when the worker died — ran on the resume

     actually spent    90,547 in /   225 out  $0.2750
     on the journal    90,547 in /   225 out  $0.2750   (record_model_usage)
     5 answers sit on the journal and the resume re-ran only what had not committed;
     durable execution is a cost control before it is anything else. The two totals
     agree because the runtime writes each attempt's usage as that attempt ends: the
     two unparseable answers are priced on their own TaskAttemptFailed events, which
     were durable before the worker died, so the crash cost the run time and not
     accounting. One caveat left: the fake answers instantly, so everything that
     started also committed, whereas a real call that returned without committing is
     billed AGAIN on the resume — and that second charge is on the journal too.
```

The crash is armed after `TaskAttemptFailed`, so the worker dies mid-fan-out at the worst moment:
two questions committed, one part-way through its retry budget, two not started. The resume picks
up exactly there. `q-pricing` and `q-references` are reused and never re-billed. `q-security`
resumes at attempt 3, having already burned two paid attempts. The two that had not started run
for the first time.

That ledger is the argument for durable execution in an agent, stated as money. A framework
without a journal re-plans and re-researches from scratch after a crash, and you pay for all of it
again.

The two totals agreeing is not the example being tidy with its own bookkeeping. Recorded usage
rides on whichever event ends an attempt — `TaskCompleted` **or** `TaskAttemptFailed` — so
`q-security`'s two rejected answers were priced on their own failure events, and those events were
durable before the worker died. The crash cost this run time, not accounting.

The one caveat the example still prints is the difference between the demo and your production run.
**The fake answers instantly**, so everything that started also committed. A real provider call
that returned a response the process died before recording is billed, and then billed **again** on
the resume. At-least-once applies to money — and both charges land on the journal, so at least the
number you read is the number you paid.

## Part 2: Nobody Approves

```console
2) the same gate, with nobody on the other side of it
   run 19fd3b5f76614e87a5fc9573f2cbac13
   status waiting — parked, holding no coroutine and no memory
   4h later, one tick: 1 run(s) woken
   status completed — escalated: no reviewer within 4h
   the wait resolved to None and the workflow chose its own branch; synthesis,
   the one call that would have cost real money, never ran.
```

The gate is `satay.wait_for_event(..., timeout=timedelta(hours=4))`. Nobody sends a decision, so
after four hours the wait resolves to `None`, the workflow takes its escalation branch, and the run
completes normally.

The placement is the design. `synthesize` sits **downstream** of the gate, so an unattended run
never pays for the expensive call. A human approval gate that costs nothing to hold open for four
hours is a budget control, and it is one primitive:
see [Timers And Events](timers-events.md) for the mechanics.

## Part 3: Fail-Fast, And Paying For Nothing

```console
3) one source never parses — fan-out is fail-fast (ADR-0020)
   run 3a4f9b9cbc0f4c58962b2f91433c177f
   run failed with MalformedResponseError: litigation: no FINDING/CONFIDENCE in a 17-token reply
   research answers that did commit: ['q-pricing', 'q-references']
   attempts burned on the dead source: 3 (retries=2, all of them billed)
     dead source       64,065 in /    51 out  $0.1930
     its siblings      18,309 in /    39 out  $0.0555
     spent, in total   82,415 in /   129 out  $0.2492
     on the journal    82,415 in /   129 out  $0.2492
   $0.1930 of that bought nothing, and all of it is on the journal:
   usage is flushed onto TaskAttemptFailed as well, so a task that never completes
   still prices itself and Studio shows what each dead attempt cost.
```

Two things worth pulling out of that, and only one of them is a limitation.

### Every attempt is priced, including the ones that failed

The run spent **$0.2492** and the journal says **$0.2492**. They agree on a run that *failed*, and
that is the point. Recorded usage is flushed onto whichever event ends an attempt, `TaskCompleted`
or `TaskAttemptFailed`. The dead source burned three full attempts, every one billed by the
provider, and every one priced on the journal even though the task never produced a result.

So **journal-derived cost is the total, not a floor** — on failed runs too. Budget alerting off the
journal is a supportable thing to build.

Reading it back is one call, and it counts failed attempts by default:

```python
from satay.journal.timeline import model_usage

model_usage(events)                                 # everything billed — $0.2492 here
model_usage(events, include_failed_attempts=False)  # only work that produced a result
```

`include_failed_attempts` is a narrowing knob, not a correction. Leave it alone for "what did this
run cost". Pass `False` for the narrower question — cost per successful item, say, where a retried
task's discarded answers are noise rather than signal.

The number that actually matters in part 3 is neither total. It is the **$0.1930 that bought
nothing**: the gap between what the run cost and what it produced. For a retry-heavy agent that is
the figure to watch, and it is a subtraction you can do now, because both sides of it are journal
state rather than one side being a guess.

Getting there takes one ordering decision in your own code, and it is the whole technique:

```python
def bill(ctx: satay.TaskContext, completion: Completion) -> None:
    """Record what the provider just charged — *before* anything can reject the answer."""
    ctx.record_model_usage(
        model=completion.model,
        input_tokens=completion.input_tokens,
        output_tokens=completion.output_tokens,
        attempt=ctx.attempt,
        usd=round(usd(completion.input_tokens, completion.output_tokens), 6),
    )
```

The example calls `bill` immediately after the model returns and **before** the parse that can
raise. Report at the moment of the charge and the retry ledger comes out right on its own, because
the runtime will flush whatever is in the slot onto the attempt's failure event. Report after the
parse and you are back to recording only the attempts that worked — not because the runtime dropped
anything, but because you never told it what the failed ones cost.

!!! warning "One attempt still goes unpriced, on purpose: an abandoned one"

    A worker death or a cancellation *mid-attempt* loses that attempt's tokens. The attempt reaches
    neither `TaskCompleted` nor `TaskAttemptFailed`, so there is no event to carry the usage — and
    nothing was committed, because the process stopped. Writing an event anyway would claim a
    durability the runtime did not deliver. Those tokens come back only when the resume re-runs the
    task, and then you are billed twice and the journal says so, which is the honest answer.

    A task that **exhausts its retries** is not this case. Every one of its attempts reaches
    `TaskAttemptFailed`, so every one is priced. That is exactly what part 3 above is showing.

Studio reads the same slot. Task detail reports usage **per attempt** and **totalled across
attempts**, so `research:litigation`'s three dead attempts each show what they cost and the logical
call shows the sum.

### Fail-fast is worse when siblings cost money

```console
   The siblings' answers do survive and a resume or fork would reuse them — but this
   workflow cannot say 'two of three answered, write it up anyway'. There is no
   collect mode, so the caller gets an exception rather than the partial result that,
   for a research fan-out, is usually the one you wanted. Getting it today means the
   task swallowing its own failure and returning a sentinel — which also gives up its
   retries, since a task that returns is a task that succeeded.
```

Same [ADR-0020](../decisions.md) fail-fast **default** as the [ELT pipeline](elt-pipeline.md), with
a worse bill attached. Two research answers committed. They are on the journal, they cost real
money, and the run is terminal so nothing can reach them.

For a research fan-out, "two of three answered, write it up anyway" is almost always the result you
wanted — and this transcript is a large part of why
[collect mode](../primitives.md#failure-fail-fast-or-collect) exists
([ADR-0027](../decisions.md)):

```python
answers = await satay.map(
    research, questions, key=lambda q: q.qid, return_exceptions=True
)
usable = [a for a in answers if not isinstance(a, Exception)]
```

The two paid-for answers come back, the run completes, and the dead source is recorded as a
terminal `TaskFailed` next to the three billed `TaskAttemptFailed` events — so it still prices
itself and Studio still shows the money it burned. What you must **not** do is the old workaround:
swallow the failure inside the task and return a sentinel. That gives up the retries that would
have rescued a transient error (a task that returns is a task that succeeded) and hides the failure
from the journal entirely.

## Part 4: Fork Under A Sharper Prompt

```console
4) fork: re-run last week's dossier under a sharper prompt
   (its own data dir: …/.satay-demo/reprompt)
   source run cedd8fb78b3b4ca7abfdf0f330ef78f6 — published
     | Recommendation: proceed.
   forked at seq 14 (just before synthesize was scheduled)
   fork run d5f855eef4ab4495a27b6ebd05a93dba — published
     | Recommendation: hold pending a second source.
   RunForked: source=cedd8fb78b3b4ca7abfdf0f330ef78f6 fork_point_seq=14
   model calls the fork actually made: ['synthesis']
     re-synthesis      56 in /    29 out  $0.0006 — the research was reused from the
     journal, not bought again. The source run is untouched and still says
     'Recommendation: proceed.'.
```

The finished dossier said `proceed`. Change the synthesis prompt from `balanced` to `sceptical`,
fork the run just before `synthesize` was scheduled, and the fork says
`hold pending a second source`.

**One model call.** Not the plan, not the research, only the synthesis. `$0.0006` against the
original run's spend. Everything before the fork point was replayed off the journal.

This is the fork loop for prompt iteration: change the prompt, fork, compare. You pay for the step
you changed and nothing upstream of it. And the source run is untouched, so you still have the old
answer to compare against.

```python
# A prompt is data, not schedule. Changing it leaves the workflow's durable-call
# sequence identical, so the fork replays cleanly under strict nondeterminism
# detection; changing which calls the workflow makes would not.
SYNTHESIS_STYLE["value"] = "sceptical"
fork_id = await control.fork(handle.run_id, fork_point)
```

!!! tip "The prompt does not have to be a global"

    This example carries the synthesis style in a module-level dict because it predates
    `workflow_input=`. Write it today and the prompt lives in the workflow's input, where it
    belongs, and the fork point is a task name rather than a computed sequence number:

    ```python
    handle = await satay.fork(
        run_id,
        before_task="synthesize",
        workflow_input={**brief, "style": "sceptical"},
    )
    print(await handle.result())
    ```

    See [Forking a run](../fork.md).

!!! warning "Changing the prompt is safe. Changing the schedule is not"

    Nondeterminism detection is **strict by default**, and it compares the durable-call schedule.
    A prompt is data flowing through a call that still happens in the same place, so the schedule
    is identical and the fork replays cleanly.

    Add a sixth research question, or reorder plan and research, and you have changed the schedule
    itself. The replay diverges and raises `NondeterminismError`. Fork accepts terminal runs only,
    and there is no automatic migration across code versions. Either let in-flight runs drain, or
    fork them.

## Open It In Studio

This example ends with `--app`, and here it earns it:

```bash
satay dev --app agentic_dag_demo --data-dir .satay-demo
```

```console
$ satay dev --app agentic_dag_demo --data-dir .satay-demo
app modules (--app): agentic_dag_demo
registered: 3 workflows (brittle_dossier, unattended_dossier, vendor_dossier); 3 tasks (plan_questions, research, synthesize)
policies: effect_safety=warn, nondeterminism=strict, version_mismatch=warn
INFO:     Started server process [769873]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:8787 (Press CTRL+C to quit)
Satay Studio:  http://127.0.0.1:8787/?token=THE_TOKEN_SATAY_DEV_PRINTED
  control/read API on http://127.0.0.1:8787  (session token required)
  press Ctrl-C to stop
```

`registered: 3 workflows` is the difference `--app` makes. Without it the dev stack serves Studio
and reads the journal but cannot start a run or wake one parked on a gate. With it, the poll loop
can wake your parked runs and `POST /runs` can start new ones. `--app` takes a **dotted module
path**, and the directory you ran from is on `sys.path`, so the bare filename works for a file you
downloaded into the current directory. From a clone of the repository it is
`--app examples.agentic_dag_demo`.

Open the printed URL with its `?token=` query string. Three things to look at:

1. **`vendor_dossier`, the published run.** In the execution tree, `research` fans out into five
   keyed items. Open `q-security` for three attempts with two recorded `MalformedResponseError`s
   and their backoff delays. Open `synthesize` for the recorded model usage.
2. **`brittle_dossier`, the failed run.** Two `TaskCompleted` for the siblings, three failed
   attempts on `research:litigation`. Open that call and each dead attempt carries what it cost,
   with the logical call totalling them — the `$0.1930` from part 3 that bought nothing, itemised.
3. **The fork pair**, which lives in its own data directory so the two runs sit side by side:

    ```bash
    satay dev --app agentic_dag_demo --data-dir .satay-demo/reprompt
    ```

    Open either run and follow the lineage chip in the header to **Compare**. It matches the two
    runs by durable-call identity rather than by sequence number, so you can see the plan and the
    research marked as replayed from the journal and only `synthesize` marked as re-run. That is
    the `$0.0006` in picture form.

Scripting it instead? Every request needs the token in an `X-Satay-Token` header. It is not
`Authorization: Bearer`, and sending a bearer token gets you the same `401` as sending nothing:

```bash
export SATAY_TOKEN="<the token satay dev printed>"

curl -s -H "X-Satay-Token: $SATAY_TOKEN" \
  'http://127.0.0.1:8787/runs/cedd8fb78b3b4ca7abfdf0f330ef78f6/compare?to=d5f855eef4ab4495a27b6ebd05a93dba'
```

## Recap

- Put every model call in a `@satay.task`. That is what makes it fakeable, replayable, and
  retryable, and it is the determinism rule doing real work.
- Deterministic Python between durable calls belongs in the workflow body and needs no journal
  entry. Anything touching a clock, a random source, or a network has to be a task.
- Satay ships no model adapters. Declare a one-method protocol, default it to a deterministic
  fake, and make the real provider opt-in.
- A crash mid-fan-out re-bills only what had not committed. That is a cost control, not just a
  correctness property.
- A `wait_for_event` gate upstream of the expensive call means an unapproved run never pays for it.
- **Every attempt is priced, including the ones that failed.** Part 3 spent $0.2492 and journalled
  $0.2492 on a run that failed. Record usage at the moment of the charge, before the parse that can
  reject the answer, and `model_usage()` gives you the total rather than a floor. Only an
  *abandoned* attempt — worker death mid-call — goes unpriced.
- Fail-fast fan-out costs more when the siblings are paid calls: committed answers survive on the
  journal but a terminal run cannot reach them. `return_exceptions=True` hands them back instead,
  and still records the dead source as a terminal `TaskFailed`.
- Forking replays everything before the fork point off the journal, so prompt iteration costs one
  call. Change data freely; changing the durable-call schedule raises `NondeterminismError`.

Next: [A Studio Tour](studio-tour.md), which builds one run touching nearly every primitive and
walks you through the debugger click by click.
