# ADR-0039 — Run deletion and reference-aware blob GC: accepting and amending ADR-0037

- **Status:** Accepted
- **Date:** 2026-08-28
- **Deciders:** Jian (leejianrong2@gmail.com)

Accepts [ADR-0037](0037-reference-aware-retention-and-blob-gc-design.md) ("a design card, not
a decision to ship") as the mechanism for the gap [ADR-0004](0004-append-only-journal.md)
named and deferred (Q54): no run deletion, no blob GC, and — because forks share blobs
(ADR-0004/Q54, [ADR-0028](0028-fork-from-code-input-override.md)) — whatever fills that gap
must be reference-aware. This ADR verifies 0037's claims against the code as it stands today,
corrects three places where that verification surfaced a drift between the design card and the
current implementation, and resolves the two questions 0037 left open. ADR-0037's own Decision
section otherwise stands; this is amendment, not a rewrite. **ADR-0037's status becomes
"Superseded by ADR-0039"** to point future readers here for the current state, the same pattern
[ADR-0020](0020-composite-failure-semantics.md)→[ADR-0027](0027-collect-mode-fan-out.md) used.

## Verification against current code

Re-read against `src/satay/blobs/__init__.py`, `src/satay/journal/store.py`,
`src/satay/control/commands.py`, `src/satay/redaction.py`, and the relevant ADRs, before
writing any implementation:

- **`BlobStore` has exactly `put`/`get`/`has`, no reference count, no index** — confirmed
  verbatim. Content-addressed as `<sha256>.blob`; `put` of existing bytes is a no-op returning
  the same id.
- **`create_fork` copies a source run's events verbatim, including blob-ref values** —
  confirmed (`src/satay/control/commands.py::create_fork`): `payload = dict(event.payload)` per
  copied event, and a blob-ref dict nested in `input_ref`/`output_ref` is carried over
  unchanged (the one exception, `workflow_input=` overriding `WORKFLOW_CREATED.input_ref`, is
  a `command.workflow_input`-provided replacement value, not a blob rewrite). Two runs' journals
  end up naming the identical hash, exactly as 0037 describes.
- **`store.list_runs()` already exists** and returns every run id, oldest first — the
  enumeration primitive the mark phase needs; no new `Store` method is required for that part.
- **Three corrections, below.**

### Correction 1 — the mark phase should walk `is_value_slot`, not a hand-rolled notion of "ref slot"

0037's mark phase says: "walk every `*_ref` value slot for a blob-ref dict." That is exactly
the traversal `satay.redaction` already implements and tests, for write-time redaction
(ADR-0029): `VALUE_REF_SUFFIX = "_ref"` and `is_value_slot(field_name)` are the one place this
codebase decides which payload fields carry values versus structure, chosen deliberately as a
suffix rule rather than a hand-maintained field list "because the list is the part that rots"
(`src/satay/redaction.py`). A GC mark phase that re-derives its own idea of "which fields to
look at" — even one that happens to agree today — is a second copy of that rule that can drift
from the first the next time an event type adds a new `*_ref` field. **The mark phase reuses
`is_value_slot` to select candidate fields, and `satay.blobs.is_blob_ref` to test whether a
selected field's value is actually a blob reference** (as opposed to an inline value that
happens to sit in a `*_ref` slot, which is the common case below the spill threshold). Both
helpers already exist and are already exercised by the redaction and spill test suites; the
mark phase adds no new traversal rule of its own.

One slot is a deliberate non-match and is confirmed safe to skip: `event_inbox.payload_ref`
(the inbox's own column, not an `events` row) is redacted on write
(`SQLiteStore.add_inbox_event`) but **never spilled** — there is no `spill_encoded` call on that
write path, only `Redactor.redact`. So a blob reference cannot originate there, and the mark
phase does not need to scan `event_inbox` at all. This is confirmed by reading the write path,
not inferred from silence — worth stating plainly since 0037 did not mention the inbox table
either way.

### Correction 2 — ADR-0036 memoisation does not speed up the mark phase

0037's Consequences section justifies the mark phase's cost by calling `read_events` "already
the fast path for reading a long journal repeatedly" after
[ADR-0036](0036-decoded-event-memoisation.md). That is true of a *resume* — a run driven
repeatedly within one process — but a GC mark phase reads each run's journal **exactly once**
per pass. ADR-0036's cache pays off on a second call for the same `run_id`; a one-shot `satay
gc` invocation opens a fresh `SQLiteStore` and calls `read_events` once per run, which is a full
decode regardless of ADR-0036. The mark phase's cost is `O(total events across every run)` on a
cold cache, exactly as 0037's Consequences section already separately (and correctly) states —
the ADR-0036 reference is an inapplicable justification for a true conclusion, not a wrong
conclusion, but it should not be cited as the reason the scan is affordable. Struck here rather
than left to mislead the implementer into expecting a speedup that will not appear.

### Correction 3 — run-deletion's terminal precondition should use `TERMINAL_STATUSES`, not a two-value list

0037's Decision 1 proposes a terminal precondition of "`completed` or `failed`," citing the fork
precedent. The actual fork precedent (`src/satay/control/commands.py`) checks membership in
**`TERMINAL_STATUSES`**, a shared constant that also includes `cancelled`
(`WorkflowCancelled`, ADR-0004's own event list). Fork already accepts a cancelled source run
as terminal; a `delete_run` that rejected one while accepting completed/failed would be a new,
unexplained asymmetry between two operations that both use the exact term "terminal." **Run
deletion mirrors fork exactly: the precondition is `record.status in TERMINAL_STATUSES`,** not
a re-typed two-value check.

## Decisions resolved

0037 named two questions as open and out of scope for the design card. Both are settled here.

**1. Deleting a run keeps its `idempotency_key` burned.** `satay.start(..., idempotency_key=X)`
must never return a different run than whatever a caller minted under `X`, including after that
run is deleted — a caller holding a stale key has no way to know a `start()` under it produced a
*new* run rather than resolving to the one it remembers. This was 0037's own tentative pick; it
is now the decision. `delete_run` does not touch the `runs.idempotency_key` uniqueness the
existing keyed-start index enforces (ADR persistence, `src/satay/journal/store.py`) — the row is
gone, but nothing re-mints a new run under the same key while any row (including a deleted
run's, if a future implementation soft-deletes rather than hard-deletes) still occupies it.
Concretely: `delete_run` **hard-deletes** the `runs` row per 0037 Decision 1, and a fresh
`get_run_by_idempotency_key` lookup after that deletion finds nothing — so the burn is enforced
by keeping the key's *history* out of scope for reuse at the call site, not by a tombstone row.
The implementation PR must add a test that starts a run with a key, deletes it, and asserts a
second `start()` under the same key mints a **new**, different run id rather than silently
resolving to the deleted one — i.e., "burned" here means "not protected from reuse by
`delete_run` returning early," the opposite failure mode of a caller expecting reuse to work.

*(Restated for clarity, since "burned" can read two ways: after `delete_run(run_id)`, a later
`satay.start(..., idempotency_key=X)` where `X` was the deleted run's key is free to mint a
fresh run — the row is really gone — but the deleted run's original identity is never
resurrected or reused for a *different* purpose. There is no dangling key blocking a legitimate
new start.)*

**2. The public surface stays CLI-only.** No `satay.delete_run()`, no `satay.gc()`, and — unlike
`fork`/`inspect`/`diff` — no importable function in `satay.control.commands` either, at least
for this first cut. 0037 already flagged a destructive operation as "arguably a stronger
candidate for staying CLI-only... than any of the read-only additions so far"; this ADR takes
that all the way rather than landing on the `satay.control.commands`-only middle ground
`create_fork`/`apply_fork` use. Rationale specific to deletion, not just "less surface is safer"
generically: every existing public primitive (`fork`, `inspect`, `diff`) is either creative
(produces a new run) or purely read-only; `delete_run`/`gc` are the first **destructive**
primitives this runtime would expose, and a script that can `import satay.control.commands` and
call a Python function is one `for run_id in ...: delete_run(run_id)` loop away from an
unattended mass-deletion with no confirmation step, whereas the CLI's dry-run-by-default and a
human at a terminal are the friction 0037's Decision 4 already designed in. If a real need for
programmatic deletion shows up later (a script-driven retention job, say), it is a small,
additive follow-up ADR — not a reason to ship the wider surface now on spec.

## Consequences

- ADR-0037's Decisions 1–4 (run deletion as a separate primitive touching only `runs`/`events`
  rows; mark-and-sweep recomputed from scratch every pass; the mtime-based grace period instead
  of a whole-pass lock; `satay gc`/`satay runs delete` as dry-run-by-default CLI verbs) are
  unchanged and now accepted, with the terminal-status check corrected to `TERMINAL_STATUSES`
  (Correction 3).
- The mark phase's implementation now has a concrete traversal to write against —
  `is_value_slot` + `is_blob_ref` over each event's payload — rather than reinventing field
  selection, and a confirmed reason to skip `event_inbox` entirely rather than an unstated one.
- CLI-only means the implementation slice needs no `satay/__init__.py` export and no
  `tests/unit/test_public_surface.py` change; `tests/integration/test_import_hygiene.py` is
  unaffected since `argparse`-only CLI code adds no new core dependency.
- Still deferred, same as 0037: encryption/secure-delete of blob bytes; a richer
  `satay runs prune` retention-policy language beyond `--older-than`/`--keep-last-N`; whether GC
  should also run under `satay dev`'s own maintenance surface. Also still deferred: an
  incremental reference count as a fast-path optimization ahead of the full mark phase, per
  0037's own Alternatives — not proposed here, and only worth reopening if the `O(total events)`
  cost is ever observed to bite at a real store size.

## Alternatives considered

- **Amend ADR-0037 in place instead of a new ADR** — rejected: this repo's own convention
  (ADR-0020→0027) is to accept/supersede with a new number rather than rewrite an existing ADR's
  body after the fact, so the history of what changed and why stays legible instead of being
  edited away.
- **`satay.control.commands.delete_run` importable, CLI-only for `gc`** — considered as a
  middle ground matching `fork`'s precedent exactly. Rejected for the reason in Decision 2:
  deletion is the first destructive primitive, and the risk profile of an unattended-scriptable
  single-run delete is different enough from a batch `gc` sweep that giving it the same
  discoverable-Python-import status as `fork` was worth departing from precedent for.
- **Free the idempotency key on deletion** — rejected per Decision 1: strictly more flexible,
  but reopens the exact silent-wrong-run risk ADR-0022's nondeterminism strictness exists to
  avoid elsewhere, for a convenience (key reuse after deletion) nothing in the current API surface
  asks for.
