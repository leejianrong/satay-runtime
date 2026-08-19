# Cookbook

Nine runnable programs, one per page. Each is a real file in the repository, each runs in CI
on every commit, and each page shows you the code, the command, and the output that command
actually printed.

You do not need to clone anything. Install the package, download one file, run it. That is
the whole setup, and it is the same three steps on every page.

## Install

```bash
pip install 'satay[studio]'
```

That is the published package from PyPI, and all nine recipes run against it.

!!! tip "These pages are pinned to one version"

    Every `curl` below fetches from the **`v0.1.0`** tag, not from `main`, so the file you
    download is the file that matches the wheel you just installed. `main` moves; the tag does
    not. [Best Of N](best-of-n.md) is the exception: its example landed after the tag, so its
    `curl` fetches from `main` and the page says so. It still runs against the wheel above.

    Satay is at `0.x`. There is no deprecation policy yet and the public API can still
    move, so pin the exact version in anything you build:
    `pip install 'satay[studio]==0.1.0'`. The [limits page](../limits.md) lists what is
    deliberately absent.

The core runtime declares **no dependencies**, and every recipe runs on it alone. The
`[studio]` extra above only adds the debugger and the HTTP API, which is the last step of most of
these pages:

```bash
pip install satay   # core only
```

## Run Any Recipe

Every recipe is a single self-contained file under `examples/` in the repository. Pick one,
fetch it, run it:

```bash
mkdir satay-cookbook && cd satay-cookbook

curl -fsSL -O https://raw.githubusercontent.com/leejianrong/satay-runtime/v0.1.0/examples/crash_recovery_demo.py

python crash_recovery_demo.py
```

That works from an empty directory. Swap the filename for any of the nine in the table
below.

If you would rather have all nine at once, clone the repository and use `uv run`:

```bash
git clone https://github.com/leejianrong/satay-runtime
cd satay-runtime
uv sync --extra studio
uv run python examples/crash_recovery_demo.py
```

### Keep The Journal

Run a recipe with no arguments and its journal goes to a throwaway temp directory. Fine for a
first look, and it means these files can run anywhere without scribbling a `.satay/` into
whatever directory you happened to be in.

To keep the journal, name a data directory. Every recipe accepts it two ways:

```bash
SATAY_DATA_DIR=.satay-demo python crash_recovery_demo.py   # environment variable
python crash_recovery_demo.py .satay-demo                  # positional path
```

Use the durable form. It is what lets you open the finished run in Studio afterwards, which is
the payoff at the end of most of these pages:

```bash
satay dev --data-dir .satay-demo
```

!!! tip "About the output on these pages"

    Every console block is pasted from a real run of the file above it. The one edit is the
    data-directory path, which is long and specific to the machine that ran it, shortened to
    `…/.satay-demo`. Run ids and timestamps are left exactly as they came out, so yours will
    differ. Where a block is an excerpt rather than the whole output, it says so.

## The Recipes

| Recipe | What it teaches |
| --- | --- |
| [Crash Recovery](crash-recovery.md) | The headline. Kill a worker mid-run, resume the same run id, watch the finished task get reused instead of re-run. |
| [Retries And Backoff](retries.md) | A task that fails twice and succeeds on the third attempt, with the whole retry schedule readable off the journal. |
| [Timers And Events](timers-events.md) | `satay.sleep` for eight hours without holding a coroutine open, `wait_for_event` / `send_event`, and the timeout branch. |
| [Fan-Out With Crash Recovery](fan-out.md) | `satay.map` over five items, two crashes, five executions in total. The keyed durable call doing its job. |
| [An ELT Pipeline](elt-pipeline.md) | Extract, transform, load with an idempotent writer, payload spill to a blob, and an honest look at fail-fast fan-out. |
| [An Agentic DAG](agentic-dag.md) | Plan, fan out research calls, gate on a human, synthesise. Why the model call has to live in a task. |
| [Best Of N](best-of-n.md) | Draft five candidates, two die, keep the three you paid for. `return_exceptions=True`, and why a collected failure is still recorded. |
| [Fork, Replay, Compare](fork-and-compare.md) | A run that completes and is *wrong*. Fork it before the bad call under a better prompt, re-run 1 of 6 calls, and diff the two runs call by call. |
| [A Studio Tour](studio-tour.md) | One run that touches nearly every primitive, then a click-by-click tour of the debugger. |

## Reading Order

Start with [Crash Recovery](crash-recovery.md) if you have not watched Satay resume a run yet.
It is the smallest file and the thing everything else is built on.

After that, [Fan-Out With Crash Recovery](fan-out.md) is the one to read. It is the same
guarantee applied to a batch, and it is the demo that tends to convince people.

[An ELT Pipeline](elt-pipeline.md) and [An Agentic DAG](agentic-dag.md) are the two long ones.
They sit closest to real workloads, and both spend as much space on what Satay does badly
today as on what it does well.

[Best Of N](best-of-n.md) is the short one of that group, and the one to read if you are
building anything that asks a model for several answers at once. It is the same fan-out under
both failure modes, a page apart.

[Fork, Replay, Compare](fork-and-compare.md) is the one to read if you are here for the
debugger rather than for durability. It is the only recipe where nothing crashes and the run
is still wrong.

If you want the concepts behind all this rather than a program to run, the
[Tutorial](../tutorial/index.md) covers the same ground in prose, and
[the determinism rule](../determinism.md) is the page to read before you write a third
workflow of your own.

## Nothing Here Waits On Real Time

An eight-hour sleep, a four-hour approval window, a full exponential-backoff schedule: these
recipes resolve all of it in microseconds. That is not a shortcut in the demo, it is the test
seam the runtime is built around.

Backoff delays and timer deadlines are measured against an **injected clock**, and jitter
comes from an **injected RNG**. Pass `satay.testing.ManualClock` and `satay.testing.SeededRng`
into `satay.start(...)` and time only moves when you call `clock.advance(...)`. The delays
that land on the journal are the real computed delays; nobody sat and waited for them.

The crashes are equally real. `satay.testing.FaultInjector` raises immediately after a named
journal event commits, so the journal is left exactly as truncated as `kill -9` would leave
it. Nothing is mocked and nothing is rolled back.

Your own tests want the same three tools. [Testing workflows](../tutorial/testing.md) is the
page for that.
