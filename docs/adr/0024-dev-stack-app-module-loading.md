# ADR-0024 — `satay dev` imports the user's app modules (`--app`)

- **Status:** Accepted
- **Date:** 2026-07-31
- **Deciders:** Jian (leejianrong2@gmail.com)

Completes the U1 acceptance criterion in [SLICE-V8](../SLICE-V8.md) ("one command runs
everything locally"). Does not change [ADR-0016](0016-core-dependency-boundary.md): the
loader is stdlib-only and lives in the studio-only `devstack` package.

## Context

The workflow registry (`satay.api.registry.REGISTRY`) is populated purely as a side
effect of `@satay.workflow` / `@satay.task` decorators executing **at import time**.
`satay dev` imported none of the user's code, so a standalone dev stack booted with an
empty registry and three things silently did not work:

- the worker's poll loop could not resolve a workflow by name, so a run parked on a
  `sleep` or a `wait_for_event` was never woken — `TimerEventWorker._redrive` returned
  early on the `workflow_def is None` branch, with no log line;
- `POST /runs` could not start any workflow, since `apply_command` resolves the name
  through the same registry;
- so timers and events only fired if the user ran their own `TimerEventWorker` inside
  their own process, which is the two-process shape the docs had to describe.

None of this was visible. `satay dev --help` advertised "the full local Satay dev stack",
the boot printed a Studio URL and looked healthy, and the failure surfaced hours later as
"nothing happened". The V8 acceptance criterion was true of the *infrastructure* and false
of *executing user code*.

The alternative was to accept that and re-document `satay dev` as an inspect-only
journal viewer. Rejected: "one command runs everything locally" is the product's pitch,
and an inspector that cannot run anything guts it. The gap is one import away.

## Decision

**1. `satay dev --app MODULE` (repeatable) imports the named modules before the stack
starts.** The import happens in `run_dev` *before* the data-directory lock is taken, so a
bad value cannot leave a half-booted stack behind. Anything the modules register is in
place by the time the worker's first tick and the control API's first request arrive.

**2. `[tool.satay] app` in `pyproject.toml` supplies the default list** so the flag need
not be retyped. Read with stdlib `tomllib` from the working directory. An explicit
`--app` wins outright — it replaces the config list rather than merging with it, so what
the command line says is what runs. The key lives in the devstack loader, not in
`satay.config`: it configures the studio-only launcher, not the runtime core.

**3. Every failure is loud and names the module.** A module that does not exist, that
imports something uninstalled, that raises during import, or that is spelled as a file
path raises `AppImportError`; `satay dev` prints it and exits 2. Silence is the bug
being fixed, so no failure mode may degrade into "boots anyway with an empty registry".

**4. The boot always states what got registered** — counts and names of workflows and
tasks — including the honest zero case, which also prints what that means ("cannot start
a run or wake one parked on a timer or event"). A typo shows up as `0 workflows` at boot
rather than as nothing firing three hours later.

**5. The project directory is *appended* to `sys.path`, never prepended.** A console-script
entry point does not put the working directory on `sys.path` the way `python -m` does, so
`--app mypkg.workflows` would otherwise fail for any project that is not pip-installed —
the common first-run case. Appending places the entry after the stdlib and site-packages,
so a stray `queue.py` or `types.py` in the project cannot shadow a stdlib module that the
runtime itself imports. Prepending would allow exactly that. The cost is that a local
package sharing a name with an installed one loses; that is the intended trade, and it is
covered by a test.

Importing user-named modules is the *point* of the feature — it is the user's own project,
run by the user, on loopback — so arbitrary code execution is not a new exposure here. The
narrow risk worth guarding is accidental stdlib shadowing, which (5) addresses.

## Consequences

- One `satay dev --app mypkg.workflows` now runs, wakes, and completes the user's own
  workflows; the two-process workaround becomes optional rather than required.
- `satay dev` grows a way to execute project code. It was already a local-only,
  loopback-bound, token-guarded developer tool (ADR-0014), and it will only import what
  the user names or configures.
- The dev stack now resolves all three project policies — `effect_safety`,
  `nondeterminism`, `version_mismatch` — from the environment and passes them to its
  worker. Previously it built the worker with none of them, so `SATAY_EFFECT_SAFETY`,
  `SATAY_NONDETERMINISM`, and `SATAY_VERSION_MISMATCH` were all silently ignored under
  `satay dev`; [ADR-0023](0023-version-mismatch-policy-split.md) recorded that gap as
  pre-existing, and this closes it. Any policy added to `TimerEventWorker` in future has to
  be threaded through `DevStack` too, or `satay dev` quietly stops honouring it.
- Multi-directory projects that are neither installed nor rooted at the working directory
  still need `PYTHONPATH`; the loader adds exactly one path entry and does not go hunting
  for `src/` layouts.
