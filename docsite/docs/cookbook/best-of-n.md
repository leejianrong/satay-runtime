# Best Of N

Draft five candidate replies under five different angles, judge them, ship the winner. Two of
the five never come back: one answers prose the parser rejects, one gets refused. You still
have three good drafts, and you paid for them.

Whether you get to keep them is one keyword argument. `satay.map` is fail-fast by default, so
the first dead candidate takes the whole fan-out with it and the three finished drafts sit on
the journal inside a terminal run that nothing can reach. `return_exceptions=True` settles
every item instead, hands the failures back beside the results, and **records each one as a
terminal `TaskFailed` event**. That second half is the part people miss, and it is what
separates collect mode from swallowing the error inside the task.

Source: [`examples/best_of_n_demo.py`](https://github.com/leejianrong/satay-runtime/blob/main/examples/best_of_n_demo.py)

!!! info "No API key, no network"

    The model sits behind a one-method protocol whose default implementation is a
    deterministic fake living in the same file. Satay ships no provider adapters on purpose
    ([ADR-0016](../decisions.md)), so this runs offline in CI and prints the same numbers on
    your laptop. Everything downstream of the model is real.

## Get It And Run It

1. Install the runtime:

    ```bash
    pip install 'satay[studio]'
    ```

2. Fetch the file:

    ```bash
    curl -fsSL -O https://raw.githubusercontent.com/leejianrong/satay-runtime/main/examples/best_of_n_demo.py
    ```

    !!! warning "This one is fetched from `main`, not from the pinned tag"

        Every other recipe pins its `curl` to the `v0.1.0` tag. This example landed after that
        tag, so there is nothing to pin it to yet. Everything it uses shipped in `0.1.0`, so
        the file runs against the wheel you just installed.

3. Run it, naming a data directory so the three journals outlive the process:

    ```bash
    SATAY_DATA_DIR=.satay-demo python best_of_n_demo.py
    ```

## The Workflow

One body, and the only thing that changes between the two runs is the `collect` flag going
into `return_exceptions=`:

```python
async def bake_off(ticket: Ticket, *, collect: bool) -> dict[str, Any]:
    candidates = [...]                      # deterministic, so it lives in the body
    outcomes = await satay.map(
        draft, candidates, key=candidate_key, concurrency=3, return_exceptions=collect
    )

    drafts = [outcome for outcome in outcomes if not isinstance(outcome, Exception)]
    rejected = [
        {"key": outcome.key, "error": outcome.error_type, "why": outcome.error_message}
        for outcome in outcomes
        if isinstance(outcome, satay.TaskFailedError)
    ]
    if not drafts:
        raise NoUsableDraftError(f"{ticket.ticket_id}: all {len(candidates)} candidates failed")

    verdict = await judge(ticket, drafts)
    ...
```

Three things in there are worth naming.

`isinstance(outcome, Exception)` is the whole unwrap. Results rejoin in **input order** as
they always do, and a failed slot holds an exception where a value would have been. This is
`asyncio.gather(return_exceptions=True)`, with a journal underneath.

A failed slot always holds `satay.TaskFailedError`, never the class the task raised. The
journal stores an error as a class *name* plus a message rather than an import path
([ADR-0005](../decisions.md)), so the original class cannot be rebuilt on replay, and a slot
whose type changed between the first pass and the replay would be nondeterminism the runtime
invented. The name still travels, on `.error_type`, and the example leans on that by having
two failure modes: both slots are `TaskFailedError`, one reports `MalformedResponseError` and
the other `RefusedError`.

The `if not drafts` branch is there because collect mode does not mean the run always
succeeds. It means the run gets to decide, and "nothing usable came back" is still a failure
worth raising.

## Part 1: The Default Kills It

```console
$ SATAY_DATA_DIR=.satay-demo python best_of_n_demo.py
Satay — best of N, and what a fan-out does when a candidate dies
data dir: …/.satay-demo
model:    fake-drafter-1 (fake, deterministic)
ticket:   T-4471 — The replacement hub arrived scratched and the box was open.

1) five candidates, fail-fast fan-out (the default)
   run fcbfa1f93c604223acbb604b6f862504
   the map raised MalformedResponseError: refund: no REPLY/CONFIDENCE in a 13-token reply
   status failed
   drafts that finished anyway: ['c-policy', 'c-goodwill', 'c-escalate']
   journal: {'WorkflowCreated': 1, 'TaskScheduled': 5, 'TaskAttemptStarted': 7, 'TaskAttemptFailed': 4, 'TaskCompleted': 3, 'WorkflowFailed': 1}
   Read that tally again. 3 drafts committed, at $0.0695, and the judge never ran. The run is
   terminal, so satay.start(run_id=…) re-raises rather than resuming and forking is
   the only way back in. No TaskFailed anywhere: under fail-fast the run's own
   WorkflowFailed is the terminal record, which is exactly what part 2 changes.
```

Three `TaskCompleted`, one `WorkflowFailed`. The drafts are real, they are durable, they cost
$0.0695, and the run they belong to is terminal, so `satay.start(run_id=...)` re-raises rather
than resuming and a prefix fork is the only way back in.

For a pipeline where every item has to land, that is the correct default and always was. For a
bake-off it is close to the opposite of what you wanted, because the entire premise of drafting
five candidates is that you only need one of them to work.

Two more things this tally says. `TaskAttemptFailed: 4` is both dead candidates spending their
full retry budget (`@satay.task(retries=1)`, so two attempts each), and every one of those
attempts is priced on the journal because usage is flushed onto `TaskAttemptFailed` as well as
`TaskCompleted`. And there is no `TaskFailed` anywhere: under fail-fast the run's own
`WorkflowFailed` is the terminal record, which is exactly the journal Satay wrote before
[ADR-0027](../decisions.md) and still writes today.

## Part 2: One Argument

```console
2) the same five candidates, with return_exceptions=True (ADR-0027)
   run c088bee8483c437495d8f40f29eea304
   status completed
   drafts judged: ['policy', 'goodwill', 'escalate']
   rejected c-refund     MalformedResponseError: refund: no REPLY/CONFIDENCE in a 13-token reply
   rejected c-legal      RefusedError: legal: I can't help with drafting that reply.
   scores policy=0.73  goodwill=0.62  escalate=0.94
   winner escalate — it answers the complaint without promising anything the policy does not.
     | Hi Priya, thanks for flagging this. We will hand the thread to a specialist who will call today.
```

Same ticket, same two dead candidates, same retry budgets. The run completes, the judge scores
the three drafts that survived, and a reply goes out.

### The Failure Is Recorded, Not Swallowed

```console
   journal: {'WorkflowCreated': 1, 'TaskScheduled': 6, 'TaskAttemptStarted': 8, 'TaskAttemptFailed': 4, 'TaskCompleted': 4, 'TaskFailed': 2, 'WorkflowCompleted': 1}
   terminal TaskFailed on: ['c-refund', 'c-legal']
     TaskFailed  key=c-refund task=draft  MalformedResponseError: refund: no REPLY/CONFIDENCE in a 13-token reply
     TaskFailed  key=c-legal task=draft  RefusedError: legal: I can't help with drafting that reply.
   That is the half of collect mode people miss. The failure is not swallowed, it
   is *recorded*: one terminal TaskFailed per dead candidate, beside the
   TaskAttemptFailed events for the attempts it burned. Retry policy, Studio, the
   read API and any cost report still see it, and a resume treats it as settled
   rather than as work to redo. The old workaround — catching the error inside the
   task and returning a sentinel — records a failure as TaskCompleted and hides it
   from all of that; examples/elt_pipeline_demo.py section 5 runs it and prices it.
```

`TaskFailed: 2`, beside `TaskAttemptFailed: 4`. Each dead candidate has one terminal record
carrying its call identity (`task_name` plus `key`) and the error as `{type, message,
traceback}`, and the four attempts underneath it are still there with their backoff delays.

This is what makes collect mode different from getting partial results by hand. Retry policy,
`effect_safety`, Studio, the read API, cost reporting and anything else reading journals all
still see two failures. The run is green because the run did complete; the two items that died
are not.

Here is the timeline of that run, from the point where the first candidate gives up (excerpt,
the run is 26 events):

```console
   15  2026-01-01T00:03:03+00:00  TaskCompleted  task=draft key=c-escalate
   16  2026-01-01T00:03:03+00:00  TaskAttemptFailed  task=draft key=c-legal attempt=1 error=RefusedError: legal: I can't help with drafting that reply. next_delay=0.316s
   17  2026-01-01T00:03:03+00:00  TaskAttemptStarted  task=draft key=c-refund attempt=2
   18  2026-01-01T00:03:03+00:00  TaskAttemptFailed  task=draft key=c-refund attempt=2 error=MalformedResponseError: refund: no REPLY/CONFIDENCE in a 13-token reply
   19  2026-01-01T00:03:03+00:00  TaskFailed
   20  2026-01-01T00:04:04+00:00  TaskAttemptStarted  task=draft key=c-legal attempt=2
   21  2026-01-01T00:04:04+00:00  TaskAttemptFailed  task=draft key=c-legal attempt=2 error=RefusedError: legal: I can't help with drafting that reply.
   22  2026-01-01T00:04:04+00:00  TaskFailed
   23  2026-01-01T00:04:04+00:00  TaskScheduled  task=judge ordinal=0
   24  2026-01-01T00:04:04+00:00  TaskAttemptStarted  task=judge ordinal=0 attempt=1
   25  2026-01-01T00:04:04+00:00  TaskCompleted  task=judge ordinal=0
   26  2026-01-01T00:04:04+00:00  WorkflowCompleted
```

!!! note "`TaskFailed` renders as a bare type line, on purpose"

    Seq 19 and 22 have no payload summary next to them. `satay runs show` is frozen at the V1
    event subset ([ADR-0016](../decisions.md)) and renders anything newer as its type alone,
    which is why the example prints the payloads itself. Studio does not draw a terminal
    marker on a collected item yet either: the run tree derives item state from
    `TaskScheduled` / `TaskAttemptFailed` / `TaskCompleted`, so you see the failed attempts and
    no verdict. Both are known gaps in the *renderers*, recorded in
    [ADR-0027](../decisions.md). The event itself is there and the read API returns it.

### What The Argument Bought

```console
   what the argument bought
     dead candidates  $0.2553  (2 of 5, billed in full, twice each)
     usable drafts    $0.0695  (3 of 5)
     this run         $0.3257 spent, one reply shipped
     part 1           $0.0695 of finished drafts, unreachable, nothing shipped
   Both runs paid for the same two dead candidates. Only one of them got anything
   for the other three.
```

Both runs spent the same $0.2553 on candidates that were never going to work, because collect
mode changes nothing about retries: an item fails only once its whole budget is gone. The
difference is the other $0.0695. In part 1 it bought three drafts nobody can read; in part 2 it
bought the three drafts the judge ranked.

For a fan-out of paid model calls that gap is the whole argument, and it grows with N. This
example is deliberately small. Draft twelve candidates against a real provider and the sibling
work you throw away for one refusal is most of the bill.

## Part 3: A Recorded Failure Replays As A Hit

The `TaskFailed` event is not only there for the reader. It is what a resume reads.

```console
3) collect mode across a crash — a recorded TaskFailed is a replay hit
   run 1f02d8f735e34a22b79fbf3dd3f4daa3
   worker died: simulated crash after event 'TaskFailed'
   attempts before the crash: {'c-refund': 2, 'c-policy': 1, 'c-goodwill': 1, 'c-escalate': 1, 'c-legal': 1}
   drafts committed:          ['c-policy', 'c-goodwill', 'c-escalate']
   terminal TaskFailed:       ['c-refund']

   restart the same run
   status completed — winner escalate
   attempts the resume added: {'c-legal': 1}
   terminal TaskFailed now:   ['c-refund', 'c-legal']
     spent before the crash  $0.2569
     spent on the resume     $0.0688
     total                   $0.3257
   ['c-refund'] had a verdict on the journal before the crash, and the resume did not
   touch it: a recorded TaskFailed is a replay hit, so the engine re-raised it
   without going near the executor. ['c-legal'] did not — one attempt recorded,
   no verdict — so the resume picked its budget up where the crash left it, at
   attempt 2, and paid for the rest of it. Partial-completion recovery, applied to a
   failure instead of to a success. The three drafts that had already committed cost
   nothing to resume either, which is the part that was true before ADR-0027.
```

The crash is armed after `TaskFailed`, so the worker dies at the moment one candidate's failure
becomes durable and the other is one attempt into its budget. `c-refund` had spent two full
attempts and been written off; the resume re-raised it straight from the journal and started no
attempt on it at all. `c-legal` had a recorded attempt and no verdict, so the resume carried on
with its budget at attempt 2.

That is the same partial-completion recovery a fan-out of successes gets, extended to the
failures. Without a terminal record for the failed item, a resumed run reads the item as
unresolved and buys the whole retry budget again.

## What Not To Do

Before collect mode existed there was one way to get partial results out of a fan-out: stop
raising. Catch the error inside the task and return an outcome object saying `ok=False`.

Do not do this. The task returned, so the runtime records `TaskCompleted`, and the failure
becomes application data that only your own code can see. Retries never fire, so a transient
error that one more attempt would have fixed is now permanent. Run status is green. Cost
attribution counts it as work. `satay runs show` and Studio show a clean run over a dead item.
You do not get partial-failure semantics out of that trade; you get no failure semantics.

[The ELT pipeline](elt-pipeline.md) runs that pattern in section 5 and prices it, side by side
with the fail-fast run in section 4. It is the evidence [ADR-0027](../decisions.md) was written
from.

## Open It In Studio

```bash
satay dev --app best_of_n_demo --data-dir .satay-demo
```

`--app` takes a dotted module path and the directory you ran from is on `sys.path`, so the bare
filename works for a file you downloaded. From a clone it is `--app examples.best_of_n_demo`.

Three runs in the list, and the pair is the point:

1. **`strict_bake_off`, failed.** Three green items under `draft`, two with failed attempts,
   and a failed run around all of them.
2. **`reply_bake_off`, completed.** The same five items, the same red attempts, and a `judge`
   call after them that the first run never reached. Open `c-refund` and you get its two
   attempts with their `MalformedResponseError`s and what each one cost. The terminal
   `TaskFailed` is in the journal and in the read API; the run tree does not mark the item as
   terminally failed yet, which is the renderer gap in the note above.
3. **`interrupted_bake_off`, completed with a ⚡.** The resume marker sits between `c-refund`'s
   last attempt and `c-legal`'s second one.

## Recap

- `map` and `gather` are fail-fast by default, which is right for a pipeline where every item
  has to land and wrong for a bake-off where one good answer is enough.
- `return_exceptions=True` settles every item and rejoins results in input order, with a failed
  slot holding the error instead of a value. It is one argument, at the call site, per fan-out.
- A failed slot is always `satay.TaskFailedError`, so the value is identical on the first pass
  and on every replay. The original class name is on `.error_type`, the identity on
  `.task_name` and `.key`.
- **A collected failure is recorded**, as a terminal `TaskFailed` beside its
  `TaskAttemptFailed` attempts. That is what keeps retries, cost reporting and the read API
  able to see it.
- A recorded `TaskFailed` replays as a hit, so a resume does not re-pay a retry budget that is
  already spent.
- Retries are unchanged: an item fails only after its whole budget is gone, and every attempt
  is priced on the journal.
- A crash is not a collected outcome. `SimulatedCrash`, `NondeterminismError` and
  `EffectSafetyError` still abort the fan-out and cancel in-flight siblings.
- Never get partial results by catching the error inside the task and returning a sentinel. It
  costs you retries, run status, and any record that a failure happened.

Next: [Fork, Replay, Compare](fork-and-compare.md), which is the other half of the debugger
story: a run that completes and is still wrong.
