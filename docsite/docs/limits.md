# Limits

Every one of these is a decision, not a bug. Knowing them before you build on Satay is worth
more than a feature list.

## Scope

**One process, one writer.** No PostgreSQL backend, no multi-worker mode, no distributed
execution. A single asyncio process owns the journal. The cross-process data-directory lock is
POSIX `flock`, and only `satay dev` takes it.

**Async only.** Sync workflows and tasks are unsupported. Every `@satay.workflow` and
`@satay.task` has to be `async def`.

**Windows is best effort.** The data-directory lock degrades to a no-op there, so nothing stops
two writers on the same journal. SQLite on a network filesystem is not supported anywhere
(ADR-0019).

## Storage

**No blob garbage collection.** No run deletion, no compaction. Spilled payloads accumulate
under `./.satay/blobs/` and removal is manual (ADR-0004). A future GC has to be
reference-aware, because a fork shares blob files with its source run.

**Redaction is on read, not on write.** The raw value is still in `satay.db`. It stops a secret
being rendered in a browser tab; it is not encryption at rest.

## Execution

**Fan-out is fail-fast only.** One failed item fails the whole `map`, `gather`, or child call,
and sibling results are discarded. There is no collect mode and no `return_exceptions`
(ADR-0020).

**Fork accepts terminal runs only** (ADR-0004): completed, failed, or cancelled. You cannot
fork a run that is still going.

**At-least-once, not exactly-once.** A task body can run more than once for one logical call.
[`ctx.idempotency_key`](guarantees.md#idempotency-keys) is the tool for making that safe, and
using it is your job.

## Correctness checking

**Nondeterminism detection is runtime-only.** There is no static analysis of workflow bodies.
Divergence is caught when a replay actually diverges, not when you write the bug.

**It compares the schedule, not arguments.** Which task at which position or key. Resume a run
with a different input and nothing complains, even in `strict` mode, while the run happily
mixes results computed from both inputs. There is a
[worked example](determinism.md#what-the-check-does-not-catch).

**No automatic migration across code versions.** Change a workflow with runs in flight and your
options are to let them drain or to fork them. The code-version stamp tells you a run was
written by different code; it does nothing about it.

## Tooling

**`satay runs show` is frozen at an early event subset** (ADR-0016). Timer, event,
cancellation, and fork events render as bare type lines with no payload summary. Studio renders
everything, and widening the text renderer is out of scope.

**`satay dev` does not import your code.** Its workflow registry is empty, so it cannot start or
wake runs of your workflows. It reads journals and writes control commands. See
[Studio and `satay dev`](studio.md#satay-dev-cannot-run-your-workflows).

**The local surface guard is not network authentication.** Session token, `Origin`/`Host`
allow-list, loopback-only bind. Proportionate for a laptop, unsuitable for a shared host
(ADR-0014).

## Maturity

`0.1.0a1` is the first published release. There is no deprecation policy, no compatibility
promise between alpha versions, and the public API can move. Pin the exact version.

Some names this documentation uses are also below the eleven re-exported from `satay`:
`TimerEventWorker`, `SQLiteStore`, `resolve_data_dir`, and `WorkflowFailedError` all live at
deeper import paths and are more likely to change. There is no high-level "run my application"
entry point yet, which is why the [worker pattern](primitives.md#running-the-worker) is
something you assemble by hand.

## Where to raise things

Issues and discussion live on
[GitHub](https://github.com/leejianrong/satay-runtime/issues). If you hit one of the items
above, the useful report is what you were trying to do, not that the limit exists.
