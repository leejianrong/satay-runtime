# Fork, Replay, Compare

An agent answers a customer. It promises a full refund the policy does not allow and cites a
policy number that does not exist. The run finishes `completed`.

That is the failure this recipe is about. Nothing raised, nothing retried, nothing to grep the
logs for — the code did exactly what it was told, and what it was told was wrong. The fix is
one word of the prompt. The question is what it costs to try.

This page forks that finished run immediately before the bad call, re-runs it under a sharper
instruction, and lays the two runs side by side call by call. **One of the six durable calls
executes. The other five answer from the journal, byte-identical.** `$0.0023` instead of
`$0.1692`.

Source: [`examples/fork_and_compare_demo.py`](https://github.com/leejianrong/satay-runtime/blob/main/examples/fork_and_compare_demo.py)

!!! info "No API key, no network"

    The model sits behind a one-method protocol whose default implementation is a
    deterministic fake living in the same file. Satay ships no provider adapters on purpose
    ([ADR-0016](../decisions.md)), so this runs offline in CI and prints the same thing on
    your laptop. Everything downstream of the model is real: the journal, the fork, the
    replay and the comparison do not know the model is fake.

## Get It And Run It

1. Install the runtime:

    ```bash
    pip install 'satay[studio]'
    ```

2. Fetch the file:

    ```bash
    curl -fsSL -O https://raw.githubusercontent.com/leejianrong/satay-runtime/main/examples/fork_and_compare_demo.py
    ```

    !!! warning "This one is fetched from `main`, not from the pinned tag"

        Every other recipe pins to `v0.1.0a3`. This example landed after that tag, so there
        is nothing to pin to yet. It needs `satay.fork`, which is also newer than the tag —
        install from the repository (`uv sync --extra studio`) if `pip install satay` gives
        you the alpha.

3. Run it, naming a data directory so the four journals outlive the process:

    ```bash
    SATAY_DATA_DIR=.satay-demo python fork_and_compare_demo.py
    ```

## The Workflow

Six durable calls: a plan, a keyed fan-out of four policy lookups, and the draft.

```python
@satay.workflow
async def answer_ticket(brief: Brief) -> dict[str, object]:
    lookups = await plan_lookups(brief)
    notes = await satay.map(look_up, lookups, key=lookup_key, concurrency=4)
    reply = await draft_reply(brief, notes)
    return {"ticket": brief.ticket, "reply": reply, **review(reply, notes, brief)}
```

`review` is the guardrail: it checks that every `POL-nn` the reply quotes was actually
retrieved, and that the reply does not promise money back outside the refund window. It is
ordinary Python living directly in the workflow body, which is allowed precisely *because* it
is deterministic — no clock, no randomness, no I/O — so replay recomputes the same verdict and
it needs no journal entry of its own.

The prompt is in the input:

```python
@dataclass(frozen=True)
class Brief:
    ticket: str
    customer: str
    question: str
    topics: list[str]
    days_since_delivery: int
    instruction: str          # <- the prompt, as data
```

That last field is the whole reason the fork below is three lines instead of a refactor. A
prompt held in a module-level global cannot be forked; a prompt held in the workflow input
can.

## Beat 1 — The Run That Went Wrong

```console
$ SATAY_DATA_DIR=.satay-demo python fork_and_compare_demo.py
Satay — the debugger loop: fork a prefix, replay, compare call by call
data dir: …/.satay-demo
model:    fake-support-1 (deterministic fake, offline)

1) the run that went wrong
   ticket TCK-8814 — delivered 35 days ago, and the refund window is 30 days
     "My blender arrived cracked. Can I get a refund?"
   instruction: "Reassure the customer and keep them happy, whatever it takes"

   run 16f3571014864692920797a93805f243 — completed
     | Hi Dana — so sorry about this! We have refunded you in full, no
     | questions asked, and a replacement is already on its way. The details
     | are all in our returns policy POL-09.

   guardrail: FAILED
     policies the run retrieved  ['POL-14', 'POL-22', 'POL-31', 'POL-40']
     policy ids the reply cites  ['POL-09']
     ids that do not exist       ['POL-09']  <- invented
     promises money back         True
     policy allows one           False  <- 35 days > 30

   Nothing raised. The run is `completed` and the workflow did exactly what it
   was told. A stack trace shows nothing and a retry produces the same answer,
   because the bug is in the input, not in the code.

     6 durable calls    55,437 in /   194 out  $0.1692
```

The order was delivered 35 days ago and the window is 30, so a refund is not on the table. The
reply promises one anyway and attributes it to `POL-09`, which the run never retrieved.

Notice what did *not* happen. No exception. No `⚡`. No failed attempt. The status is
`completed` and the result object is well-formed. This is the class of bug that durability
alone does not help with — and it is most of the bugs in an agent.

The `$0.1692` is the four policy lookups. Each one drags a retrieved document along, which is
why the research is 98% of the bill and the draft is the other 2%. Remember that ratio; the
rest of the page is about not paying it twice.

## Beat 2 — Fork Before The Bad Call

```console
2) fork it immediately before the bad call, under a sharper instruction
     satay.fork(run_id, before_task="draft_reply", workflow_input=sharper)
   instruction:
     > Answer only from the notes below, quote the policy id behind every
     > claim, and never promise an outcome the notes do not allow

   fork run 907387252c3d4e6f90b7a2db99f51df3 — completed
     | Hi Dana — thanks for the details. Items damaged in transit are covered
     | for 30 days from delivery [POL-14]. Refunds are issued inside 30 days of
     | delivery, and store credit after that [POL-22]. An order number is proof
     | enough and no receipt photo is required [POL-31]. A like-for-like
     | replacement may be offered before a refund [POL-40]. Your order was
     | delivered 35 days ago, so the 30-day window has closed and I cannot
     | issue a refund; I can offer store credit or a like-for-like replacement
     | instead.

   guardrail: PASSED
     policies the run retrieved  ['POL-14', 'POL-22', 'POL-31', 'POL-40']
     policy ids the reply cites  ['POL-14', 'POL-22', 'POL-31', 'POL-40']
     ids that do not exist       []
     promises money back         False
   RunForked: source=16f3571014864692920797a93805f243 fork_point_seq=16 input_overridden=True
```

The call that produced it:

```python
sharper = replace(REFUND_TICKET, instruction=GROUNDED_INSTRUCTION)

handle = await satay.fork(
    source_id, before_task="draft_reply", workflow_input=sharper, store=store, clock=clock
)
result = await handle.result()
```

`before_task="draft_reply"` cuts the journal so that call re-runs, without you scanning event
sequence numbers to find it. `workflow_input=` supplies the new brief, and it is written into
the fork's *own* `WorkflowCreated` event rather than held in memory — so a fork that parks on a
timer and wakes an hour later still reads back the input it actually ran under. The `RunForked`
line is that lineage, readable off the journal: which run it came from, where it was cut, and
that the input was overridden.

The source run is untouched. [ADR-0004](../decisions.md) makes a journal immutable; a fork is a
new run seeded with a copy of a prefix, never an edit.

## Beat 3 — The Number

```console
3) what the fork actually re-ran
   durable calls it executed           1  ['draft_reply:0']
   durable calls it read off the copy  5

     the source run    55,437 in /   194 out  $0.1692
     the fork             151 in /   124 out  $0.0023

   >>> 1 of 6 durable calls re-ran; 5 were reused byte-identical.
   >>> $0.0023 to fix the answer, against $0.1692 for the original run —
   >>> 98.6% of the bill was history, and history does not need re-buying.
   >>> The source run is untouched and still says what it said.
```

"Executed" here is not the demo's own bookkeeping. A fork's journal opens with a **verbatim
copy** of its source's prefix, attempt events included, so the test for "did this run actually
enter the function body" is whether a `TaskAttemptStarted` sits *above* the `RunForked` marker:

```python
def executed_here(events):
    boundary = next((e.seq for e in events if e.type is EventType.RUN_FORKED), 0)
    return [call_identity(e.payload)
            for e in events
            if e.seq > boundary and e.type is EventType.TASK_ATTEMPT_STARTED]
```

One name comes back. The costs are read the same way — `record_model_usage` entries above the
marker, because the entries below it are the source's charges, copied rather than repeated.

## Beat 4 — Compare, Call By Call

```console
4) compare, call by call
     ReadAPI.compare(16f35710…, 90738725…)
     GET /runs/16f3571014864692920797a93805f243/compare?to=907387252c3d4e6f90b7a2db99f51df3

   durable call                       source     fork       recorded output
   plan_lookups:0                     completed  completed  identical — replayed
   look_up:key:damaged-on-arrival     completed  completed  identical — replayed
   look_up:key:refund-window          completed  completed  identical — replayed
   look_up:key:proof-of-purchase      completed  completed  identical — replayed
   look_up:key:replacement-vs-refund  completed  completed  identical — replayed
   draft_reply:0                      completed  completed  DIFFERS  <- the fixed call

   6 calls aligned on both sides; 5 identical, 1 different — and the
   one that differs is the one call after the fork point. Studio draws this
   table; the JSON behind it is what you just read off two real journals.
```

Compare aligns two runs by **durable-call identity** — `plan_lookups:0` is the first call to
`plan_lookups`, `look_up:key:refund-window` is the fan-out item with that key — and gives each
side that call's status and recorded output. An identity present on one side and absent on the
other shows as a hole, which is how a structural divergence reads.

Here there are no holes. Six rows, six aligned pairs, five with equal recorded output. The
table is not a diff of two log files; it is the journals themselves, aligned by the identity
the replay engine uses.

!!! warning "The query parameter is `to`, not `other_run_id`"

    Over HTTP the route is `GET /runs/{run_id}/compare?to={other}` and the parameter is
    **required**, so `?other_run_id=` comes back `422`. The Python method is
    `ReadAPI.compare(run_id, other_run_id)`. The two spellings diverge on purpose — `to` reads
    as prose in a URL the path has already scoped to runs — and every URL this example prints
    is replayed against the real server by `tests/e2e/test_example_urls.py`, so if the page
    ever lies about it, the build goes red.

## Beat 5 — The One Rule

A fork's copied prefix is a record of calls that already happened. `workflow_input=` therefore
reaches only the calls **after** the fork point. Get that wrong and you get a confident answer
built on last week's research:

```console
5) the rule: a fork's copied prefix is history, not a prediction
   Same fork point, but a different QUESTION this time — an address change,
   not a refund. `draft_reply` is the only call after the cut, so it is the
   only call that sees the new ticket:

   fork run 5794a5da01214892add6c2e63d911d87 — completed, guardrail PASSED, and useless
     | Hi Dana — thanks for the details. Items damaged in transit are covered
     | for 30 days from delivery [POL-14]. Refunds are issued inside 30 days of
     | delivery, and store credit after that [POL-22]. An order number is proof
     | enough and no receipt photo is required [POL-31]. A like-for-like
     | replacement may be offered before a refund [POL-40]. I can issue a
     | refund.
     durable calls it executed:  ['draft_reply:0']
     policies on its journal:    damaged-on-arrival, refund-window, proof-of-purchase, replacement-vs-refund
   Every citation is real, so the guardrail passes. The research is simply
   answering the previous question, because those four calls already happened
   and a fork reuses them rather than paying for them again.
```

That is worth sitting with. The question was about changing a delivery address. The reply
discusses damaged goods and offers a refund, every citation is genuine, and the guardrail is
perfectly happy. Reuse is not free of judgement: it reuses whatever you told it to reuse.

The fix is to cut earlier:

```console
   Fork before `plan_lookups` instead and the new input reaches everything:

   fork run 18b1ce1954ff4f6d96c36f57e6cb3207 — completed, guardrail PASSED
     | Hi Dana — thanks for the details. A delivery address can be changed
     | until the carrier scans the parcel [POL-51]. Once scanned, only the
     | carrier can redirect a parcel and it may refuse [POL-58]. After handoff
     | the label cannot be altered and the order returns to sender [POL-63]. A
     | reshipment after a failed delivery carries a flat handling fee [POL-70].
     | Tell me which option suits you and I will set it up.
     durable calls it executed:  6 of 6    51,966 in /   252 out  $0.1597
     policies on its journal:    address-change, in-transit-redirect, carrier-handoff, reshipment-fee

   So: put the fork point before the first durable call that should see the new
   input (ADR-0028). `before_task=` exists to let you say exactly that, and the
   full-price run above is what it costs when the honest answer is 'all of them'.
```

Six of six calls re-ran and the fork cost full price, which is correct: the whole run had to
change. The rule in one sentence — **put the fork point before the first durable call that
should see the new input** — and `before_task=` is how you say it. [Forking a run](../fork.md)
has the rest, including what happens when the new input would make the workflow call
*different* tasks inside the copied prefix (it raises, rather than splicing two histories into
a plausible wrong answer).

## The Closing Line

```console
four runs of a six-call workflow, and this process made 14 model calls,
not 24. The other 10 answered from the journal.

journals kept in …/.satay-demo
open all four runs:  satay dev --app examples.fork_and_compare_demo --data-dir …/.satay-demo
  then compare 16f3571014864692920797a93805f243 against its fork 907387252c3d4e6f90b7a2db99f51df3
or as text:          satay runs show 16f3571014864692920797a93805f243 --data-dir …/.satay-demo
```

## Open It In Studio

```bash
satay dev --app examples.fork_and_compare_demo --data-dir .satay-demo
```

`--app` matters here in a way it does not for the other recipes: it imports the workflows, so
Studio can fork a run itself rather than only showing you one. Open the printed URL with its
`?token=` query string.

In the run list you will see four runs of `answer_ticket`, one root and three forks, each with
a lineage line pointing back at the source. Open the bad one, click "fork before here" on
`TaskScheduled task=draft_reply`, and you have done from the UI what beat 2 did from Python.
Then "Compare runs" against the source draws the beat 4 table.

## What This Is Not

Satay ships **no agent abstractions** ([ADR-0025](../decisions.md)): no loop framework, no tool
protocol, no provider adapters, no graph DSL. This page teaches a pattern with five durable
primitives and a `dataclass`; it does not ship one. The model client, the retrieval corpus, the
prompt templates and the guardrail are all yours, and in this example they are all visible in
the one file so you can see where your own would go.

## If You Are Recording This

The page is laid out as five beats because that is a screencast. What to run and what to say:

| Beat | On screen | The one sentence |
| --- | --- | --- |
| 0 | The workflow body, six lines | "Six durable calls. The prompt is in the input." |
| 1 | Section 1 of the output | "It finished. It's wrong. Nothing raised." |
| 2 | The three-line `satay.fork` call, then section 2 | "Fork before the bad call, sharper prompt." |
| 3 | Section 3 | "One call re-ran. Five were reused. Two tenths of a cent." |
| 4 | Section 4 | "And here is exactly what diverged, call by call." |
| 5 | Section 5 | "The prefix is history — so cut before the first call that should change." |
| 6 | `satay dev`, the run list, fork-before-here, compare | "All of that, locally, with no account." |

Run the file once with `SATAY_DATA_DIR` set before recording so Studio has something to open,
then run it again on camera; the output is deterministic, so the run ids in the terminal will
be the only thing that differs between takes. The whole script takes under a second, so the
pacing is yours, not the runtime's.

## Recap

- A run can complete and still be wrong. Durability does not catch that; a debugger does.
- `satay.fork(run_id, before_task=..., workflow_input=...)` re-cuts a finished run from code.
  No control API, no command queue, no worker tick, and no `satay[studio]` extra.
- The copied prefix is replayed, not re-executed, so the fork pays only for what comes after
  the cut. Here: 1 of 6 calls, `$0.0023` against `$0.1692`.
- Compare aligns two runs by durable-call identity, so "what changed" is a table rather than an
  argument.
- Because the prefix is history, the new input reaches only the un-replayed suffix. Put the
  fork point before the first call that should see it.

Next: [An Agentic DAG](agentic-dag.md), which puts a human approval gate in front of the
expensive call and shows the same fork loop inside a longer story.
