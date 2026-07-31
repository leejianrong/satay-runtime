# Tutorial - User Guide

This tutorial shows you how to use Satay, one feature at a time. It is written to be read in
order: each page assumes the page before it and adds a single idea.

You will start by killing a running workflow and watching it resume without redoing the work
that already finished. Then timers, external events, fan-out, and the rule that keeps all of it
correct. By the end you will have a test that crashes a workflow on purpose and skips a
fourteen-day sleep in under a tenth of a second.

Every code block on these pages was executed to produce the output shown underneath it.

## Install

```bash
pip install satay
```

Or with [uv](https://docs.astral.sh/uv/), which is what the project itself uses:

```bash
uv venv
source .venv/bin/activate      # .venv\Scripts\activate on Windows
uv pip install satay
```

Either way you get exactly one package:

```console
$ pip list
Package Version
------- -------
pip     26.2
satay   0.1.0a2
```

The core install is dependency-free. That is the packaging promise, and it is why you can embed
Satay in an application without dragging FastAPI, uvicorn, and a JavaScript bundle into
production. The debugger and the HTTP API are a separate opt-in:

```bash
pip install 'satay[studio]'     # adds fastapi, uvicorn, pydantic, typer
```

Check the CLI landed:

```console
$ satay --help
usage: satay [-h] {runs,dev} ...

Satay Runtime — local-first durable execution (core CLI).

positional arguments:
  {runs,dev}
    runs      Inspect durable runs.
    dev       (studio extra) Boot the local dev stack; --app MODULE imports
              your workflows.

options:
  -h, --help  show this help message and exit
```

!!! warning "Pin the version"

    Every page in this tutorial was written and executed against **`0.1.0a2`**, the current
    release. Satay is alpha: there is no compatibility promise between alpha versions and
    nothing is deprecated gracefully yet, so pin the exact version in anything you build.

    ```bash
    pip install 'satay[studio]==0.1.0a2'
    ```

    [Limits](../limits.md) lists what is deliberately missing.

## Requirements

Python 3.12 or 3.13. Linux and macOS are first class. Windows is best effort: the
cross-process data-directory lock uses POSIX `flock` and degrades to a no-op elsewhere. SQLite
on a network filesystem is not supported.

## The Pages, in Order

| Page | What it adds |
| --- | --- |
| [First Steps](../quickstart.md) | Two tasks, one workflow, a real crash, and a resume. |
| [Concepts](../concepts.md) | The journal, replay from the top, and how a call keeps its identity. |
| [The Determinism Rule](../determinism.md) | The one rule that makes replay work, and what breaking it looks like. |
| [The Five Primitives](../primitives.md) | `sleep`, `wait_for_event`/`send_event`, `map`, `gather`, `start_child`. |
| [Testing Workflows](testing.md) | The manual clock, the seeded RNG, and crashing a workflow in a test. |

## Then the Cookbook

The tutorial builds small pieces to explain one idea at a time. The
[Cookbook](../cookbook/index.md) does the opposite: each recipe is a complete program you can
run, with the journal or Studio output that proves it worked. Go there once you know what you
want to build.

Lookup material sits in [Guarantees](../guarantees.md) and
[Studio and `satay dev`](../studio.md). Read those when a question comes up, not front to back.

## Start Here

[First Steps](../quickstart.md) takes about ten minutes and gets you the crash-and-resume story
first hand.
