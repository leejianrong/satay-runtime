# ADR-0037 — Reference-aware retention and blob GC: a design card, not a decision to ship

- **Status:** Proposed — a design card. Nothing in this ADR is implemented; it exists so a
  future implementation PR has a design to build against instead of starting from a blank page.
- **Date:** 2026-08-27
- **Deciders:** Jian (leejianrong2@gmail.com)

Roadmap item 8, the gap [ADR-0004](0004-append-only-journal.md) named and deferred: "A retention
/ `satay gc` policy is post-MVP and, because forks share blobs, must be reference-aware." Related
to, but distinct from, the "retention" [ADR-0026](0026-license-and-hosted-journal-plane.md) names
as a tier-1 **hosted** feature — see Decision 6. Depends on nothing else being reopened: this
proposes new capability (run deletion and blob GC do not exist today, at all), not a change to
anything already shipped.

## Context

Two things are true today and neither is an oversight:

- **There is no way to delete a run.** `fork` reads a terminal run and writes a new one; nothing
  removes a run's rows from `runs` or `events`. The read paths (`inspect`, `diff`, the HTTP read
  API) all assume every `run_id` they were ever handed still resolves.
- **There is no blob GC.** `BlobStore` (`src/satay/blobs/`) has `put` / `get` / `has` and nothing
  else — no reference count, no "who points at this," no index of any kind. Blobs accumulate
  under `.satay/blobs/` forever; manual `rm` is the only escape hatch ADR-0004 offers today.

The reason this is harder than "delete the row, delete the file" is content addressing. A blob
file is named `<sha256-of-its-bytes>.blob` (`src/satay/blobs/__init__.py`), and a journal payload
references it by that hash, not by a path or an owning run id. `BlobStore.put` of identical bytes
is a no-op that returns the same id — dedup is automatic and free. `fork` (ADR-0028) copies a
source run's events **verbatim** into the new run's own rows, so a spilled payload's blob-ref
dict (`{"id": <hash>, "size": N}`) is copied byte-for-byte too: both runs' journals now name the
identical hash, and neither owns the file more than the other. Two *unrelated* runs that happen to
spill identical content share it the same way, coincidentally. There is no "owning run" concept
for a blob to hang deletion off of — only "is this hash still named by anyone's journal."

One clarifying fact worth stating plainly, because it simplifies the design: **deleting a run's
own SQL rows does not endanger any other run's replay.** A fork's copied prefix lives in the
fork's *own* `events` rows from the moment it is created — it does not read through to the
source's rows at replay time. So a source run can be deleted while its forks live on, and the
forks keep working; the only shared, at-risk resource is the blob *files* on disk, not the
journal rows. Run deletion and blob GC are two different operations with two different safety
questions, and conflating them is where a naive design goes wrong ("delete run → delete its
blobs" is exactly the unsound shortcut ADR-0004 already ruled out).

## Decision

This card proposes a design in four parts. **None of it is built.** A future PR implementing any
part of this should treat this ADR as a starting draft, not a spec frozen in place.

**1. Run deletion is a new, separate primitive — `satay.control.commands.delete_run` (name not
final) — deleting one run's `runs` and `events` rows, and nothing else.** It does not touch blobs.
It does not cascade to children or forks. Proposed preconditions, mirroring the fork precedent
(ADR-0004: "fork accepts terminal runs only"):

- The run must be terminal (`completed` or `failed`) — never delete a run mid-flight out from
  under a driver that might still resume it.
- No cascading. A parent whose child was deleted keeps a `child_run_id` that no longer resolves;
  a fork whose source was deleted keeps its lineage's `source_run_id` pointing at nothing. Both
  already have to tolerate an unknown `run_id` gracefully (`RunNotFoundError`, `LookupError`) for
  other reasons, so this is a consequence to document, not a new failure mode to invent.
- **Open question, not resolved here:** does deleting a run started with an `idempotency_key`
  free that key for reuse, or does the key stay burned? Reusing it risks `satay.start(...,
  idempotency_key=X)` silently returning a *different* run than whatever a caller remembers
  minting under that key; leaving it burned is simpler and safer, and is this card's tentative
  recommendation, but it is a product decision, not an implementation detail, and belongs in the
  PR that actually builds this.

**2. Blob GC is mark-and-sweep, run separately from any single deletion, and safe to run any
time — including in a store where nothing has ever been deleted.**

- **Mark:** read every remaining run's full journal (`store.read_events`, which — after
  ADR-0036 — is already the fast path for reading a long journal repeatedly) and walk every
  `*_ref` value slot for a blob-ref dict, collecting the set of every hash still named by
  anyone's journal, anywhere in the store.
- **Sweep:** for every `<hash>.blob` file under `.satay/blobs/` whose hash is *not* in the marked
  set, delete it.
- No per-run "owns this blob" index needs to be maintained incrementally to make this correct —
  the mark phase rebuilds the true reference set from scratch every time, the same way ADR-0036's
  cache rebuilds its index from the journal rather than trying to track it incrementally through
  every write path. Correctness by recomputation, not by bookkeeping that could drift.

**3. Concurrency safety: a grace period, not a lock held across the whole sweep.** This is a
single-writer, single-process system (ADR-0007/0012), but a GC pass can still take real wall-clock
time over a large store, and a new run can spill a brand-new blob while a mark phase is scanning.
Holding a lock for the whole pass would stall the one writer for the GC's duration, which is a
worse cost than the alternative: **the sweep only deletes a `.blob` file whose filesystem mtime is
older than the mark phase's own start time** (plus a configurable buffer, e.g. a few minutes). Any
blob spilled during or after the mark phase is protected by its own recency regardless of whether
the mark phase happened to see the run that wrote it. This is the standard technique conservative
mark-and-sweep collectors use for exactly this race, applied here instead of a lock.

**4. `satay gc` is explicit, user-invoked, and dry-run by default.** No background scheduler runs
it; nothing in this codebase deletes bytes without being asked to (ADR-0004's own "manual removal
is the escape hatch" framing already sets that expectation). Proposed shape:

```console
$ satay gc --data-dir .satay
would reclaim 41 blobs, 18.2 MB (312 blobs, 4.1 GB still referenced)
re-run with --apply to delete them

$ satay gc --data-dir .satay --apply
reclaimed 41 blobs, 18.2 MB
```

A companion `satay runs delete <run_id>` (or `prune`) is the natural place to put the roadmap
note's "retention" — an `--older-than` / `--keep-last-N` convenience that selects which terminal
runs to delete, built on the single-run primitive from Decision 1, not a new policy engine.

**5. This is a policy question layered on a mechanism, and the mechanism is what this card scopes.**
"Delete runs older than 30 days" and "delete runs beyond the last 100" are both just different
selectors feeding the same `delete_run` primitive; neither belongs baked into the mechanism.

**6. Local blob GC and hosted "retention" are related but distinct, and this card is only the
first.** ADR-0026 decision 1 names blob GC as an **open-core** capability — it ships to every
self-hosted user, free, same as everything else. ADR-0026's tier-1 "retention" is the *hosted
journal plane's* own data-lifecycle policy (how long an ingested journal lives on hosted infra),
which is a different system this local mechanism does not touch — but the roadmap note's "cannot
sell retention without it" is still right: no hosting implementation exists yet (ADR-0026), and
whatever a hosted retention policy eventually does — expire old ingested journals, purge on
request — needs *some* reference-aware deletion mechanism underneath it, local or hosted-side. This
card is the local half of that prerequisite, built because it stands on its own as a genuine local
need (a debugger whose journal directory only ever grows is a real, if not urgent, problem for a
long-lived local user) — not because hosting is imminent.

## Consequences

- **Nothing changes for an existing user until a future PR implements part of this.** No code, no
  tests, no public surface in this card — it is pure design.
- **The public surface question is deferred, deliberately.** Whatever ships — a `satay.delete()`,
  a `satay.control.commands` function only, a bare CLI verb with no Python entry point — should go
  through the same public-surface discipline the last three cards did (CLAUDE.md: push back on
  growing `satay.*` without a strong reason). A destructive operation is arguably a stronger
  candidate for staying CLI-only, or living beside `fork` in `satay.control.commands` rather than
  the bare top-level namespace, than any of the read-only additions so far — flagged here for
  whoever picks this up, not decided.
- **The mark phase is `O(total events across every remaining run)`, every time it runs**, since it
  recomputes rather than tracks incrementally. Fine at the scale ADR-0025 targets (local-first,
  one user, no multi-tenant store); worth a second look if a store ever holds enough runs for that
  full-store scan to be slow in its own right. Not addressed here — a future incremental index is
  a legitimate follow-up if the naive scan turns out to matter, the same "revisit if it bites"
  posture ARCHITECTURE §9 already takes with replay cost.
- **A dangling `child_run_id` / `source_run_id` after a deletion is a new kind of "not found" a
  reader has to expect.** `inspect`, `diff`, and the tree/compare views already raise or 404 on an
  unknown run id for other reasons (a typo, a run in a different store); this adds one more cause
  to a category of failure that already exists, rather than inventing a new one.
- **Deliberately not addressed here, and named so a future reader does not read the silence as an
  oversight:** encryption/secure-delete of blob bytes (a `rm` on a local filesystem is not a
  shredding guarantee, and this card does not change that); a `satay runs prune` retention policy
  language beyond the two examples in Decision 4; whether GC should also run under `satay dev`'s
  own maintenance surface rather than only as a standalone CLI verb.

## Alternatives considered

- **Reference-count blobs incrementally, maintained on every write** — rejected as the primary
  mechanism: it requires every write path that can create or copy a blob-ref (`append`, `fork`,
  and any future one) to remember to update a count, and a missed call site silently corrupts the
  count in the unsafe direction (undercounting → a live blob gets swept). Mark-and-sweep recomputes
  from the source of truth every time and cannot drift the same way. An incremental count could
  still be added later purely as a *fast path* to skip a full mark phase when nothing has changed
  since the last one — not proposed here, but compatible with this design if the O(total events)
  cost in Consequences ever actually bites.
- **Delete a blob when its "owning" run is deleted** — rejected outright: this is exactly the
  unsound shortcut ADR-0004 already named and ruled out (Q54), because content addressing means a
  blob frequently has no single owner.
- **Cascade deletion to forks and children** — rejected: forks are self-sufficient after creation
  (Context), so cascading would delete data that is still perfectly valid and replayable, for no
  correctness reason — only to avoid a dangling reference that the read paths already have to
  tolerate for other reasons.
- **A background/scheduled GC** — rejected for v1: nothing in this codebase deletes anything
  without being asked, and a local debugging tool whose entire value proposition is "nothing is
  ever silently gone" is the wrong place to introduce a first automatic deletion. An explicit,
  dry-run-by-default CLI verb keeps that property intact.
- **Lock the store for the whole GC pass instead of a grace period** — rejected: it is simpler, but
  stalls the one writer for the GC's duration, and the grace-period technique achieves the same
  safety without that cost, at the price of a small, bounded window where a very recently deleted
  run's blob is not reclaimed until the next pass.
