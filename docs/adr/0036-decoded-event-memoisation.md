# ADR-0036 — Decoded-event memoisation in `SQLiteStore`, per process

- **Status:** Accepted
- **Date:** 2026-08-27
- **Deciders:** Jian (leejianrong2@gmail.com)

Roadmap item 7 after [ADR-0025](0025-positioning-agents-first.md): `docs/ARCHITECTURE.md` §9
named this directly — "add decoded-result memoisation within a process's lifetime" — as the
lightweight mitigation to reach for before the heavier one ("continuation snapshots"), and said
it "does not change [ADR-0001](0001-event-sourced-replay.md)." This decision is that mitigation,
built exactly to that brief and no further. It touches no public surface: `SQLiteStore` is not
exported from `satay/__init__.py`, and `read_events`'s signature and return type are unchanged.

## Context

Every drive of a run — a resume, a wake from a timer or event, a crash recovery — replays the
workflow body from the top (ADR-0001), and every drive starts with `read_events(run_id)`
(`src/satay/api/runner.py`, `src/satay/replay/engine.py`) fetching the run's *entire* journal from
SQLite. `SQLiteStore.read_events` (`src/satay/journal/store.py`, pre-existing) read every row for
the run on every call and ran each one through `_decode_payload`: a `json.loads`, blob-reference
rehydration, and the codec's recursive tag-walk decode. None of that is cheap per byte, and none
of it changes between two calls that see the same prefix — a run's recorded events are immutable
once written (ADR-0004: no deletion, no compaction), so decoding event 3 twice produces the exact
same `Event` object's worth of information both times.

The actual cost is `O(N)` per drive, for a journal of `N` events, and a run that resumes `R` times
pays `O(N·R)` in read-plus-decode work across its life — even though the replay engine's own
per-drive work (building the completed/failed/scheduled index, per-call lookups) is a single
`O(N)` pass with `O(1)` dict lookups per durable call, not the bottleneck. "Agent loops write the
longest journals" (the roadmap note) because they combine both dimensions: many sequential
durable calls (large `N` — every tool call, every planning step) *and* frequent resumes (large
`R` — a wait on an external event or a timer is itself a drive when it wakes, and a poll-heavy
agent loop wakes often). `O(N·R)` is exactly the product this decision removes the `R` from.

## Decision

**1. `SQLiteStore` caches decoded events per run, in memory, for its own lifetime.** A new
`dict[str, tuple[Event, ...]]` on the instance. `read_events(run_id)` reads the cached tuple's
last `seq`, fetches only rows with a greater `seq`, decodes only those, and returns
`cached + new` — extending the cache and returning the same extended tuple, which becomes the
next call's cache. A run this store instance has never read pays the full cost once, the same as
today; every call after that pays only for what changed.

**2. The cache is never invalidated, only extended.** The journal is append-only (ADR-0004): once
written, an event's payload never changes and is never removed. A cached prefix is therefore a
fact that stays true for the life of the process — there is no staleness case to handle, no
version counter, no TTL.

**3. Scope is exactly "a process's lifetime," as the architecture note specified.** The cache
lives on the `SQLiteStore` instance, not anywhere durable (not in SQLite, not on disk, not in a
module global). A fresh `SQLiteStore.open(...)` — a new process, or a second store opened against
the same file — starts with an empty cache and pays full price on its first read of each run, same
as before this change. Nothing here reopens the "one process, one writer" model (ADR-0007/0012)
or the store-seam assumptions ADR-0001 protects; it is an implementation detail entirely inside
one method of one class.

**4. Returned as an immutable `tuple`, not a `list`.** `read_events`'s declared return type
(`Sequence[Event]`) is satisfied by either, and every existing caller either already wraps the
result in `list(...)` before mutating anything (`ReplayEngine.drive`) or only iterates it
read-only — verified by grep, not assumed. Returning the tuple itself on a cache hit, rather than
a defensive copy, is what keeps a repeat read `O(1)` in the common case instead of paying an
`O(N)` copy to undo the very saving this decision makes: `Event` is already a frozen dataclass, so
the only mutable surface was ever the containing list, and a tuple removes it.

**5. Guarded by the existing per-run `asyncio.Lock`** (`self._run_locks[run_id]`, already used by
every write method here). Not strictly required today — `read_events` has no internal `await`
point, so two calls for the same run can never actually interleave on one event loop — but the
lock documents the invariant for a reader who does not want to re-derive that from first
principles, matches this file's own established idiom for every other per-run critical section,
and costs nothing while there is no contention to protect against.

## Consequences

- **A repeat drive of an unchanged run is `O(1)` instead of `O(N)`** (the identity check in
  `tests/integration/test_store.py::test_read_events_hands_back_the_same_object_when_nothing_is_new`
  pins this, not just the values). A drive after `k` new events is `O(k)` for the fetch-and-decode,
  plus an unavoidable `O(N)` to assemble the returned tuple — cheap pointer copies, not re-decoding
  — so a run's total read-plus-decode cost across `R` resumes drops from `O(N·R)` to `O(N)`
  amortized (each event decoded exactly once, ever, per process).
- **No eviction.** A long-lived process (a `satay dev` server, a long-running worker script)
  handling many distinct runs over its life accumulates their decoded journals in memory for as
  long as the process runs; the cache never shrinks. This is the same deliberate-gap shape
  ADR-0004 already accepts for the store itself ("no run deletion, no compaction") — in RAM
  instead of on disk, and not addressed here for the same reason the roadmap note gives it a
  qualifier at all: "irrelevant at MVP scale." Worth revisiting if a real long-lived-process,
  many-runs deployment shape shows up; no evidence of one yet, and ADR-0025 keeps the near-term
  positioning single-process, no multi-worker.
- **`read_events`'s behavior is unchanged from the outside.** Same signature, same ordering, same
  content for a given `run_id` at a given moment; the full existing suite (655 tests, including
  every crash-recovery, resume, fork, and fan-out test that drives a real `ReplayEngine`) passes
  with zero test changes required beyond the new tests added for the cache itself — the strongest
  available evidence this is additive, not a behavior change dressed as one.
- **`ReplayEngine`'s own per-drive cost is untouched.** Its index build (`_load_journal`) is
  already a single `O(N)` pass with `O(1)`-per-call lookups against `_completed` / `_failed` —
  confirmed while scoping this card, and not a bottleneck this decision needed to touch.
- **Continuation snapshots remain future work**, exactly as the architecture note left them. They
  would change what gets *replayed*, not merely how fast a journal is *read*, which is a
  materially bigger change (partial replay, snapshot invalidation, a new persisted artifact) than
  the brief for this card.

## Alternatives considered

- **Cache inside `ReplayEngine` instead of `SQLiteStore`** — rejected: a fresh `ReplayEngine` is
  constructed for every drive (confirmed in `src/satay/api/runner.py`), so it has no lifetime
  longer than one drive to cache across. The store is the object that actually persists across
  drives within a process, which is exactly the scope ARCHITECTURE §9 asked for.
- **Cache at write time** (update the cache inside `append`, rather than incrementally on read) —
  rejected: it would couple cache maintenance to every write call site (`append`, and anywhere
  else that ever appends), instead of the one read path, for no benefit — the read-time
  incremental fetch is self-healing by construction and cannot drift from what is actually in
  SQLite, whereas a write-time cache could silently diverge if a future write path forgot to
  update it.
- **An LRU or size-bounded cache** — rejected for this card: no eviction policy exists anywhere
  else in this codebase, no evidence yet that the unbounded-growth case matters at the scale
  ADR-0025 targets, and it is real added complexity (a policy to choose, a size to tune, a test
  matrix for eviction correctness) for a problem that has not been observed. Named as a gap above,
  not silently dropped.
- **Continuation snapshots now, instead of memoisation** — rejected: the architecture note is
  explicit that memoisation is the thing to try first ("if it ever bites, add decoded-result
  memoisation ... (and, later, continuation snapshots)"), and snapshots are a materially larger
  change for a cost this MVP-scale card does not yet need to pay.
