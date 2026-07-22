# Satay Runtime — Grilling Questions

> Mode 1 (initial grilling). Confirm-only run: the decisions in
> `initial_planning_summary.md` are treated as accepted. These questions targeted
> the **open framing choices** and **genuine architectural forks / tensions**
> the summary left unresolved.
>
> **Status: all questions ANSWERED (2026-07-20).** Outcomes distilled into
> `CONTEXT.md` (glossary + decision register) and `docs/adr/*.md`.
>
> Priority: **P0** blocks architecture · **P1** shapes a stage · **P2** sensible-default.

---

## Topic 1 — Workflow execution & replay mechanism (the core)

**Q1 [P0] [ANSWERED] — Replay model.**
Event-sourced replay (Option A): on resume the workflow function re-runs top-to-
bottom; each durable call returns its journaled result if present, else executes
and appends. Coroutine snapshotting rejected — requires pickle/opaque state and
breaks the JSON-durable + no-pickle + inspectable-journal principles. → ADR-0001.

**Q2 [P0] [ANSWERED] — Durable-call identity.**
Sequential call-site ordinal + task-definition name (Option A). `satay.map`
items keyed by explicit `key=`. Argument-hashing rejected (collisions + silent
drift). Optional explicit `id=` escape hatch is a possible later add. → ADR-0002.

**Q3 [P0] [ANSWERED] — Nondeterminism detection.**
Runtime-only for the MVP (Option A). On a replay mismatch raise
`NondeterminismError`; dev = warn + offer fork, strict = hard-fail. Static AST
analysis of workflow bodies deferred post-MVP (leaky + costly). → ADR-0003.

## Topic 2 — Async & concurrency model

**Q4 [P1] [ANSWERED] — Async-only?**
Yes. MVP supports async-only workflows/tasks; sync work is wrapped in a thread by
the user. Sync support deferred. → ADR-0007.

**Q5 [P1] [ANSWERED] — MVP worker model.**
Single-process asyncio worker. `TaskExecutor` interface exists day one; only
`LocalTaskExecutor` ships. `satay.map(concurrency=N)` = asyncio concurrency in
one process. Multi-worker / Postgres-claimed execution is post-MVP. → ADR-0007.

## Topic 3 — AI-awareness scope

**Q6 [P0] [ANSWERED] — Model/token/cost metadata.**
Tasks self-report via `ctx` (e.g. `ctx.record_model_usage(model, in, out)`). No
SDK auto-instrumentation (explicit non-goal). Journal stores a generic usage/cost
slot, not a model-specific schema. → ADR-0008.

**Q7 [P1] [ANSWERED] — Model adapters in MVP?**
Core ships none; the reference app calls a provider SDK directly inside tasks and
self-reports usage. Keeps "runtime before ecosystem" honest. → ADR-0008.

## Topic 4 — Serialization & type reconstruction

**Q8 [P1] [ANSWERED] — Rehydrating typed results on replay.**
Use the task's return type annotation to rehydrate stored JSON (Pydantic
`model_validate`, dataclass reconstruction); fall back to plain dict when
unannotated. Typed replay therefore requires annotated task returns. → ADR-0005.

**Q9 [P2] [ANSWERED] — Large payloads.**
Inline JSON in the journal up to ~256 KB; above that spill to a blob store (local
files in dev) and store a reference. Threshold/backend tunable later. → ADR-0004.

## Topic 5 — Local surfaces (debugger, control API, events)

**Q10 [P0] [ANSWERED] — Debugger UI form factor.**
Local web app (Option A) served over a JSON API by `satay dev`. The API seam
lets a TUI follow later at low cost. → ADR-0009.

**Q11 [P1] [ANSWERED] — Control API + event delivery.**
Local HTTP control API writes to the store (`start`/`status`/`cancel`/
`send_event`); the worker picks up events and timers by polling. Same JSON API
that Studio reads from. → ADR-0009.

**Q12 [P2] [ANSWERED] — Timers/sleep.**
`satay.sleep` and event-wait timeouts persisted as timer rows; worker polls due
timers (~1s in dev). No external scheduler. → ADR-0004 / ADR-0007.

## Topic 6 — Policy defaults

**Q13 [P2] [ANSWERED] — Retry defaults.**
`retries=0` unless specified; when >0, exponential backoff with jitter (base 1s,
cap ~60s). `effect_safety="warn"` in dev, with `"strict"`/`"off"` available.
→ ADR-0006.

**Q14 [P1] [ANSWERED] — Code-version identifier default.**
Fallback chain: git commit if in a repo, else a developer-provided string, else a
content hash of the workflow source. → ADR-0010.

## Topic 7 — Product packaging

**Q15 [P1] [ANSWERED] — Name lock-in.**
Proceed under **Satay** (package `satay`, CLI `satay`, debugger "Satay Studio")
now, flagged provisional pending PyPI / domain / trademark checks. A later rename
is a mechanical find-replace. → CONTEXT decision register (D-name).

**Q16 [P2] [ANSWERED] — License & Python version.**
**Apache-2.0** license. **Python 3.12+** minimum (user amended from 3.11 — gains
newer typing niceties in addition to `TaskGroup`/`ExceptionGroup`).
→ CONTEXT decision register (D-license, D-python).

**Q17 [P1] [ANSWERED] — MVP deliverable boundary.**
MVP = runtime (all 5 primitives) + SQLite + Satay Studio + the two-task
crash-recovery vertical slice (summary §22) as the proof. The vendor-dossier app
is the **next** milestone, built on the finished runtime. → docs/PRD.md scope.

---

### Answered summary

All 17 questions answered on 2026-07-20. P0 forks: event-sourced replay,
call-site-ordinal identity, runtime-only nondeterminism detection, ctx
self-report for model usage, Studio as a local web app. P1/P2: async-only,
single-process asyncio worker, no core model adapters, annotation-based
rehydration, HTTP control API + polling, version fallback chain, name `satay`
(provisional), Apache-2.0 + Python 3.12, MVP = runtime + slice (dossier app next).

---

# Mode 2 — Architecture / tech-stack grill (G2)

> Opened 2026-07-20 against `ARCHITECTURE.md` (esp. §12 "Proposed" rows and the
> three open questions) and the default assumptions in §2-§8. These are **not yet
> answered**. Numbering continues from Mode 1 (Q18+). Each records: the current
> first-pass default, the tension, the options, and my lean. Priority: **P0**
> blocks/reshapes the architecture · **P1** shapes a stage · **P2** sensible-default.

### Decisions so far (2026-07-21, Jian) — ADRs pending
- **Q18 / Q19 / Q28 → DECIDED: Option C.** Control+read API runs on its **own
  thread**; it **reads SQLite directly** (WAL concurrent reads) but **routes writes
  through an in-process command queue to the worker, which stays the sole writer**.
  Single-writer story preserved; debugger stays responsive under load. **SQLite
  kept for MVP**; Postgres remains the post-MVP production backend (Q28 declined).
  Reconsidered 2026-07-21 on a "Postgres + docker-compose from day one" argument and
  **declined again**: Option C already removed the writer motivation, and the docker
  dependency would erode the local-first / zero-infra wedge. Postgres's real payoff
  (multi-worker) is post-MVP; it arrives additively via the `Store` seam.
- **Q20 / Q21 / Q25 → DECIDED: lean core.** `satay` core stays (near) pure-Python;
  Pydantic support is **duck-typed, not a hard dependency**; FastAPI + uvicorn + the
  built Studio bundle move behind a **`satay[studio]` extra**, assets prebuilt in CI
  (never at `pip install`). FastAPI-vs-alternatives (Q25) finalized when the extra is
  specced.
- **Q22 → DECIDED (Jian, 2026-07-21):** auth-lite on the loopback API — random port,
  a session token `satay dev` prints and Studio sends per request, and an
  `Origin`/`Host` allow-list. Not a login system.
- **Q23 → DECIDED:** MVP ships the four core Studio views (run list; timeline +
  interruption marker; execution tree; task/attempt detail). Fork, run-compare, and
  the version-mismatch banner are deferred. Frontend = **Svelte + Vite (plain SPA)**,
  with the timeline and tree drawn via a framework-neutral lib (d3).
- **Q24 → DECIDED (lean):** dedicated writer thread over stdlib `sqlite3`, benchmarked
  against `aiosqlite` before final lock; pragmas set by hand (`journal_mode=WAL`,
  `synchronous=NORMAL`, real `busy_timeout`). Dissolves if the store ever moves to
  Postgres.
- **Q25 → DECIDED:** **FastAPI + uvicorn**, living inside the `satay[studio]` extra
  (supersedes the "finalize later" note above; consistent with lean core since
  FastAPI/Pydantic never enter the core dependency set).
- **Q26 → DECIDED:** **mypy** (strict); **pytest-asyncio**; drop `dulwich` (git binary
  if a repo is present, else a source hash).
- **Q27 → DECIDED (docs only):** replay cost recorded as O(journal length) per drive in
  ARCHITECTURE §9; no change to ADR-0001.
- **Tooling prefs (Jian, 2026-07-21):** `uv` for env/deps; **Ruff** for lint/format;
  **FastAPI + uvicorn** for the API (studio extra); **Vitest** for frontend unit tests
  (the MVP still verifies Studio behaviour through the JSON API per ADR-0011).
- **Roadmap:** three-phase roadmap (SQLite → Postgres → multi-worker) now recorded in
  ARCHITECTURE.md §9.

**All G2 questions (Q18–Q28) are now resolved. Write-up complete (2026-07-21):**
ADRs 0012-0015 created; each inline tag flipped to `[DECIDED]` with an ADR pointer;
decisions reflected in ARCHITECTURE (§1, §2, §3.3, §3.6, §3.7, §4.1, §6, §7, §8, §9,
§11, §12), CONTEXT (glossary + D12-D16), and PRD (implementation decisions + Studio
MVP scope). The question bodies above keep their original first-pass framing (e.g.
"React", "in-loop") as the historical record; the resolutions are the tags and the
decisions block.

## Topic 8 — Co-hosting the HTTP API on the worker loop (§2, §3.6)

**Q18 [P0] [DECIDED · ADR-0012] — In-loop uvicorn vs. a separate-thread API.**
Default: FastAPI+uvicorn started programmatically **on the worker's own asyncio
loop**. Tension: the worker loop also runs user task coroutines. Tasks are "where
all I/O lives" and are `async`, but any accidental blocking (a sync SDK call, a
big JSON/Pydantic decode, CPU work) stalls the *whole loop* — freezing Studio and
the control API exactly when a run is busy and you most want to inspect it. That
contradicts §2's own claim that "the worker never blocks on the HTTP layer": the
coupling runs the *other* way. Since §2 also says the journal is the only shared
truth, the API needs **no in-memory access to the worker** — so a separate thread
(or process) with its own loop and its own SQLite connection would be *more*
aligned with the stated architecture, not less, and would isolate debugger
responsiveness from worker load. Options: (A) in-loop as drafted; (B) API in a
second thread reading/writing the store; (C) API in a separate process.
**Lean: (B).** But it collides with Q19 — read them together.

## Topic 9 — The single-writer story (§2, §3.3, §4.1)

**Q19 [P0] [DECIDED · ADR-0012] — "Single writer under an async lock" vs. the control API as a
second writer.**
§3.3 says `seq` is allocated "under an async writer lock" with a single writer.
But §2 says external writes (`start`/`cancel`/`send_event`/`fork`) "land in the
store" and the worker polls them up — i.e. **the control API writes to SQLite
too.** That is already two writers. SQLite (even WAL) permits only one writer at a
time, so either (a) the API shares the worker's connection + lock (only possible
if they're on the same loop — which *argues for* Q18-A, the opposite of my Q18
lean), or (b) the API is a distinct writer and we accept `SQLITE_BUSY` +
`BEGIN IMMEDIATE` retry handling. **These two P0s are one decision:** in-loop
single-writer (simple persistence, stalls under load) vs. separate-thread API
(responsive debugger, multi-writer SQLite contention to manage). Which property
wins for a *debugging* tool?
Sub-question: is the append lock **store-wide or per-run**? Store-wide serializes
all appends across all runs (a throughput ceiling once `satay.map`/child
workflows fan out); per-run is finer but SQLite's own single-writer constraint
makes it partly moot. Confirm the intended granularity and that it's acceptable
for MVP fan-out.

**Q28 [P0] [DECIDED · ADR-0012] — Postgres from day one instead of SQLite? (raised by Jian
2026-07-20)**
Postgres MVCC allows genuine concurrent writers, so it dissolves the *SQLite-
specific mechanical* half of Q19 (no `SQLITE_BUSY` dance, no single-physical-
writer limit) and even offers `LISTEN/NOTIFY` to replace ~1s polling with push.
**But** (1) it contradicts the accepted persistence ordering (CONTEXT: SQLite =
MVP local default; Postgres = first *production* backend post-MVP) and guts the
local-first "`pip install satay && satay dev`, zero infra" wedge — a dev now needs
a running Postgres just to see the two-task demo; (2) it does **not** answer the
real, backend-independent design question underneath Q19 — *does the control API
write run state directly, or enqueue a command the worker applies?* Postgres just
lets you get away with two physical writers instead of forcing that choice.
**Lean: keep SQLite for MVP; resolve Q19 with a backend-portable pattern (the
`Store` seam already exists for exactly the SQLite→Postgres move).** Logged for a
real decision, not assumed.

## Topic 10 — Pydantic's place (§3.1, §3.3, §12)

**Q20 [P1] [DECIDED · ADR-0013] — Pydantic v2 as a *core* dependency vs. optional + API-only.**
Default: Pydantic v2 for (a) journal/codec typed validation + rehydration and (b)
API response models. Tension: Pydantic v2 is a heavy compiled dep (pydantic-core
/ Rust), version-sensitive in users' own projects, and §3.1 prides the author API
on "no third-party dependency." Rehydration only needs Pydantic *when the user's
return type is itself a Pydantic model* — which can be handled by duck-typing
(`hasattr(rt, "model_validate")`) with an **optional** import, so Satay supports
Pydantic without depending on it; stdlib dataclasses/TypedDict/enum/datetime need
no Pydantic at all. If the journal *event* models (`TaskScheduled`, …) are also
Pydantic, every append validates through it on the hot path. Options: (A) hard
core dep as drafted; (B) **core stays stdlib-only; Pydantic support is duck-typed;
Pydantic/FastAPI live behind a `satay[studio]` extra** (couples to Q21/Q25).
**Lean: (B)** — a runtime people embed in their apps should have a near-zero core
dependency surface.

## Topic 11 — Studio packaging & build (§1, §3.7, §6, §12)

**Q21 [P1] [DECIDED · ADR-0013] — Studio bundle shipped in the core wheel vs. an extra / separate
package.**
Default: Vite-built React bundle packaged as wheel data files so `pip install
satay` "just works." Tensions: (1) **everyone who imports Satay as a library
ships a JS SPA in their production deploy**, even though they only run `satay dev`
locally — bloat for a runtime dep; (2) **build reproducibility** — hatchling now
needs a Node/Vite step, so either built JS is committed to the repo (ugly) or
wheels are Node-built in CI and the sdist can't rebuild the frontend without Node
on the user's machine. Options: (A) in the core wheel as drafted; (B) **`satay`
core stays pure-Python, Studio ships in `satay[studio]` (FastAPI+uvicorn+assets)
or a separate `satay-studio` package**; prebuilt assets baked in CI, never built
at `pip install`. **Lean: (B)** for the same "lean embeddable core" reason as Q20.

**Q22 [P1] [DECIDED · ADR-0014] — "Localhost ⇒ no auth" is not automatically safe for a
*browser* tool (§7).**
Default: control+read API binds loopback, no authN/authZ. Gap: a browser app on a
predictable localhost port is exposed to **CSRF and DNS-rebinding** — any website
the developer visits can POST `http://127.0.0.1:<port>/cancel` or `/send_event`
(writes have no auth), and rebinding can bypass same-origin for reads. On shared
dev boxes, any local user can also drive the API. Cheap MVP mitigations that don't
add a real auth system: **random port + a per-session bearer token printed by
`satay dev`, plus `Origin`/`Host` header allow-listing.** Options: (A) truly no
auth as drafted; (B) loopback + session token + Origin check in MVP. **Lean: (B)**
— it's a few lines and closes a real, well-known local-service hole.

**Q23 [P1] [DECIDED · ADR-0013 + PRD] — MVP Studio scope drives the frontend weight (§3.7, §12 open
Q3).**
The open question "how much Studio to build" is upstream of "React vs Svelte vs
plain TS." Decide **which of the 7 views ship in the MVP** (run list, timeline w/
interruption marker, execution tree, task/attempt detail+usage — vs. fork,
compare, version-mismatch banner). The MVP slice only strictly needs list +
timeline + tree + task detail. The heavy views (compare, tree viz) are the ones
that justify a framework at all. Once scoped: is React+Vite the right weight, or
is the timeline/tree rendering the real cost (a viz approach question) more than
the framework choice? **Lean: scope MVP to the 4 core views; pick the framework
after.**
Note (2026-07-21, Jian): **Svelte + Vite** is under active consideration vs.
React + Vite. Fully viable — framework sits behind the API seam (ADR-0011), so
it's reversible; Svelte suits the lean-core/small-bundle goal. Caveat: the heavy
views (timeline/tree/compare) are the real cost and lean on framework-neutral viz
(d3) more than on the framework choice. Use **plain Svelte + Vite SPA, not
SvelteKit**. Decide with the scope question above; tie-break on Jian's fluency.

## Topic 12 — Smaller "Proposed" rows (§3, §8, §12)

**Q24 [P2] [DECIDED · ADR-0012] — `aiosqlite` vs. a dedicated writer-thread over stdlib
`sqlite3`.**
aiosqlite is a thread-per-connection wrapper: every query thread-hops via a queue,
adding latency on the per-event append hot path. Given Q19 already implies a
purpose-built single writer + lock, we may be reimplementing aiosqlite's threading
anyway — a dedicated writer thread with stdlib `sqlite3` would give explicit
control of the writer lock, `seq` allocation, and optional **append batching** for
throughput, with separate read-only WAL connections for Studio. Options: (A)
aiosqlite as drafted; (B) dedicated writer thread + stdlib sqlite3. Confirm which,
and confirm the WAL `PRAGMA` set (`journal_mode=WAL`, `synchronous=NORMAL` vs
`FULL` — durability-vs-speed on the append path) and `busy_timeout`.

**Q25 [P2] [DECIDED · ADR-0013] — FastAPI+uvicorn vs. Starlette-only / Litestar / raw ASGI.**
Coupled to Q18/Q20/Q21. If Pydantic leaves the core (Q20) and the only client is
Studio tested through JSON payloads (ADR-0011), FastAPI's main draws (Pydantic
models, OpenAPI) matter less, and uvicorn's value is mostly its ASGI protocol impl
since we're not using it as a process manager. Options: (A) FastAPI+uvicorn; (B)
Starlette-only + uvicorn/hypercorn; (C) Litestar. Decide after Q18/Q20.

**Q26 [P2] [DECIDED · ADR-0015] — Small toolchain confirmations.**
(a) Type checker: **mypy strict** vs **pyright** (§8 flags this for G3). (b) Test
runner: **pytest-asyncio** vs **anyio** — anyio pairs well if any Trio/portability
intent, otherwise pytest-asyncio. (c) Code-version: is **`dulwich`** worth a
dependency, or do "git binary if present, else source hash" cover it (drop the
middle rung)? **Leans: mypy strict; pytest-asyncio; drop dulwich.**

## Topic 13 — Architecture note downstream of a Decided ADR

**Q27 [P2] [DECIDED · ARCH §9] — Replay cost grows with journal length (ADR-0001, not being
reopened).**
Event-sourced replay re-runs the workflow top-to-bottom on every drive, reusing
journaled results. For long/looping workflows with many durable calls, each
resume/poll-driven wake replays all prior calls — O(journal) per drive. Irrelevant
to the two-task MVP slice, but the architecture names no mitigation (decoded-result
memoization within a process's lifetime, or a future continuation/snapshot). Not a
request to reopen ADR-0001 — just: should ARCHITECTURE.md §9 record this as a known
scaling characteristic + future item, and confirm each store poll does **not**
trigger a needless full replay of already-waiting runs?

---

# Mode 3 — Tech-stack gaps (G3)

> Opened 2026-07-21 against the post-G2 `ARCHITECTURE.md`. G2 challenged the choices
> that were made; **G3 hunts the choices not yet made**: languages, libraries,
> frameworks, environments, deployment, persistence, APIs, third-party services.
> Numbering continues (Q29+). Same priorities (P0/P1/P2); P2 items carry an `ASSUMED`
> default the user can override. **Not yet answered.**

## Topic 14 — The core dependency boundary

**Q29 [P1] [DECIDED · ADR-0016] — Does the CLI live in the core, and is Typer a core dependency?**
`satay dev` needs the `satay[studio]` extra (it serves Studio), but `satay runs show`
is read-only text and is useful without the debugger, so it arguably belongs in the
lean core. Typer pulls in click (and rich), which is a non-trivial surface for a core
that we just worked to keep near-stdlib. Options: (A) Typer in the core; (B) a minimal
**argparse/click** core CLI for `satay runs show`, with Typer (and `satay dev`) only in
the extra; (C) all CLI in the extra, core is library-only. **Lean: (B)** — keep a tiny
stdlib-only core CLI; gate `satay dev` on the extra with a clear error if it's missing.

**Q30 [P1] [DECIDED · ADR-0016] — With Pydantic out of the core, what defines and validates the
journal event model?** The codec and the event types (`TaskScheduled`, …) need a
representation in the core. Options: **stdlib frozen dataclasses** (zero dep, manual
light validation, fine because the worker is the only producer); **attrs** (dep);
**msgspec** (small, fast `Struct` + JSON, could serve both codec and API, but still a
third-party dep). **Lean: frozen dataclasses in the core**; consider msgspec only if
encode/decode throughput demands it, and only behind the codec seam.

**Q31 [P1] [DECIDED · ADR-0016] — Raw SQL, or an ORM / query builder?** Nothing states this. An ORM
(SQLAlchemy) is a heavy core dependency and fights the dedicated-writer-thread control
we chose in ADR-0012. **Lean: raw parameterized SQL over stdlib `sqlite3`, no
SQLAlchemy.** Confirm.

## Topic 15 — Persistence specifics (unspecified in ADR-0004/0012)

**Q32 [P1] [DECIDED · ADR-0017] — Default on-disk location and layout for the SQLite DB + blob dir.**
"a directory under the run's data path" is never pinned. Options: a project-local
**`./.satay/`** (git-ignorable, zero dep, obvious in dev) with a `--data-dir` override;
or a user-global path via **`platformdirs`** (small dep, better for a shared install).
**Lean: `./.satay/` by default + `--data-dir`**, avoid `platformdirs` for the MVP.

**Q33 [P1] [DECIDED · ADR-0017] — Schema migration strategy across `satay` versions.** The journal is
long-lived, so the DB schema will outlive a single release. Options: hand-rolled,
stepwise migrations keyed on **`PRAGMA user_version`** (zero dep); a migration tool
(Alembic is SQLAlchemy-shaped, overkill here). **Lean: hand-rolled `user_version`
steps.** Confirm, and decide the policy for an older DB opened by newer code.

## Topic 16 — The read API surface and Studio liveness

**Q34 [P2] [DECIDED · ADR-0018] — How does Studio get fresh data, and is the JSON API
versioned?** Default assumption: **Studio polls the read API on an interval** (mirrors
the worker's poll model), no SSE/WebSocket in the MVP; and because the server and
Studio ship together in one extra, the API is **not formally versioned** (FastAPI still
emits OpenAPI for free). Override if you want live push (SSE) in the MVP.

## Topic 17 — Frontend stack specifics

**Q35 [P1] [DECIDED · ADR-0018] — Svelte version, package manager, and Node version.** ADR-0013 picks
Svelte + Vite but not the versions. Options: **Svelte 5 (runes)** vs 4; **pnpm** vs npm
vs yarn; a pinned Node LTS in CI. **Lean: Svelte 5, pnpm, Node LTS pinned**, since the
bundle is CI-built and never built at `pip install`.

**Q36 [P2] [DECIDED · ADR-0018] — CSS and routing for the four-view SPA.** Default:
**plain CSS (or CSS modules), minimal client-side routing** (or just conditional view
rendering); no Tailwind for an app this small. Override if you want a utility-CSS or a
router.

## Topic 18 — Environments, platforms, and release

**Q37 [P1] [DECIDED · ADR-0019] — Supported OSes and Python matrix for the MVP.** Unstated, and it
matters: SQLite WAL does not work on network filesystems and has Windows quirks.
**Lean: Linux + macOS first-class, Windows best-effort, local disk only (documented);
test on Python 3.12 and 3.13** in CI.

**Q38 [P2] [DECIDED · ADR-0019] — PyPI release mechanics.** Default: **GitHub Actions with
OIDC trusted publishing** (no long-lived tokens), building the sdist + wheel (with the
CI-built Studio bundle vendored). Override if you prefer `twine` + an API token.

## Topic 19 — Runtime cross-cutting confirmations

**Q39 [P2] [DECIDED · ADR-0019] — Logging, retry impl, and coverage tooling.** Defaults:
runtime logging via **stdlib `logging`** under a `satay` logger (no structured-logging
dep); **retry/backoff hand-rolled** as already drafted (driven by the injected clock,
keeps lean core and testability, no `tenacity`); **pytest-cov** for coverage in CI, with
**hypothesis** as an optional add for the codec and idempotency-key derivation. Override
any of these.

---

### G3 write-up complete (2026-07-21)

All Mode-3 questions (Q29–Q39) resolved; Jian accepted all leans. New ADRs:
**ADR-0016** (core dependency boundary), **ADR-0017** (persistence layout &
migrations), **ADR-0018** (frontend & Studio delivery), **ADR-0019** (platform,
release & tooling). Rippled through ARCHITECTURE (§1, §3.3, §3.6, §3.7, §3.9, §4.1,
§4.2, §6, §8, §12), CONTEXT (D17–D20), and PRD (implementation decisions + modules +
out-of-scope). Inline tags above point at each ADR. This closes step **G** of
`/build-plan-specs`; next is step **H** (grill the test plans → `TESTING.md`).

---

# H2 — Test plan audit (2026-07-21)

These questions arose from auditing the `## Test Plan` section of every SLICE-V*.md
against ADR-0011 (the test seam of record). Findings are written up in `TESTING.md`;
each question below cross-references its finding there. All are **OPEN** pending H3.

## Topic 20 — Test structure & the ADR-0011 seam

**Q40 [P1] [DECIDED · ADR-0011 H3 · lean B accepted] — Should the integration tier be narrowed to boundary-only tests, or
kept as E2E mirrors?** In V2/V3/V4/V7/V8 nearly every integration test restates an E2E
test one level down (TESTING.md finding A). Because ADR-0011 makes the public API the
primary seam, the E2E tier already covers those paths, so the twin adds cost without
coverage. Options: (A) keep the current mirrored tiers (redundant but defensive);
(B) recast the integration tier to *only* tests that isolate a component boundary the
E2E cannot reach (store `seq`, codec, resolver, backoff, inbox, poll loop, redactor),
deleting pure restatements. **Lean: (B)** — it matches ADR-0011's "primary seam is the
public API" and keeps the suite honest about where coverage actually comes from. This
may add a clarifying note to ADR-0011.

**Q41 [P2] [DECIDED · ADR-0011 H3 · lean B accepted] — Are internal-spying integration tests allowed, or must they be
reframed via observable markers?** ADR-0011 says "never on replay internals," yet tests
like V1's "returns a recorded result on a journal hit *without invoking the executor*"
assert the executor was not called (TESTING.md finding B). Options: (A) sanction
internal spying as a deliberate, documented exception for a few engine tests; (B) reframe
all of them to assert via the execution-count marker + journal (observable). **Lean:
(B)** — the markers already exist in every slice and keep the seam philosophy intact.

**Q51 [P2] [DECIDED · ADR-0011 H3 · lean A accepted] — Add an explicit test that reads succeed while the worker is
stalled?** ADR-0012's headline property — "the debugger never blocks on the worker" — is
untested (TESTING.md finding F). Proposal: a V5 test that uses the fault-injection hook
to pause the worker mid-write and asserts a read endpoint still returns promptly over a
WAL read-only connection. **Lean: add it to V5**; it is the one test that proves the
two-thread design earns its complexity.

## Topic 21 — Cross-slice ownership & the read-API contract

**Q42 [P1] [DECIDED · ADR-0009 H3 · lean B accepted] — Define the interruption/"gap" marker precisely, and own it in one
place.** The ⚡ marker is rendered by the V1 CLI and independently by V6 (view model +
unit), and "a `WorkflowResumed` follows a gap" is never defined (TESTING.md finding C).
Options: (A) keep the fuzzy "gap" heuristic, detected per-surface; (B) define it as
"mark on every `WorkflowWaiting`→`WorkflowResumed` transition," compute it once (in the
read-API/view layer), and test it once, with the CLI and Studio both consuming it.
**Lean: (B)** — precise, single-owner, and testable with one unit test.

**Q43 [P1] [DECIDED · ADR-0014 H3 · lean A accepted] — Where do the ADR-0014 security tests live, and who issues the
session token?** The token + `Origin`/`Host` allow-list + loopback/random-port guards
are untested everywhere (TESTING.md finding E). The answer depends on the token's
issuer: if only `satay dev` issues it, the guard is exercised from V8; if the V5 server
issues it whenever the HTTP surface is up, the negative tests (missing/invalid token →
reject; bad `Origin`/`Host` → reject; non-loopback bind → refuse) belong in V5.
**Lean: the V5 server owns the guard and its tests** (the surface exists from V5;
`satay dev` in V8 just wires a token into it), so the negative tests are V5 with a V8
smoke test that `satay dev` supplies a working token.

**Q44 [P1] [DECIDED · ADR-0009 H3 · lean A accepted] — Which slice owns the compare endpoint implementation and its
tests?** Compare is built and unit-tested in V5 (build step 5) yet also fully claimed by
V7 (affordance "N16 compare: Full", E2E/integration) — TESTING.md finding H. Options:
(A) endpoint + all tests in V5, V7 adds only the Studio side-by-side *view*; (B) V5
leaves a stub (like `fork`) and V7 implements the endpoint + view together. **Lean:
(A)** — the read endpoint is pure over the journal and belongs with the other read
endpoints in V5; V7 owns only the UI, mirroring how V5 already splits the `fork` route
(stub) from V7's fork semantics. Then compare's endpoint tests are V5, its view tests V7.

**Q45 [P2] [DECIDED · ADR-0018 H3 · lean A accepted] — Treat the read-API JSON contract as additive rather than "fixed."**
V5 calls the contract "fixed/load-bearing," but V2 (usage slot), V4 (tree linkage), and
V7 (version-mismatch field, `RunForked` lineage) all extend it (TESTING.md finding D),
and V7's mismatch banner has no field to read unless one is added. **Lean: declare the
contract additive and forward-compatible**, enumerate in V5 the fields V2/V4/V7 will add,
and make V6 view tests tolerant of added/unknown fields so they don't break when V7 lands.

**Q50 [P2] [DECIDED · ADR-0016 H3 · lean A accepted] — Is `satay runs show` frozen at the V1 event subset, or extended per
slice?** The CLI is tested only for V1 events; V3/V4/V7 add event types it would render
but never re-test (TESTING.md finding J). Options: (A) freeze the CLI at the V1 subset
for the MVP (state it, and Studio is the surface for the rest); (B) extend the CLI per
slice with one rendering test per new event family. **Lean: (A)** for MVP — the CLI is
the bootstrap inspector, Studio (V6+) is the real timeline; document the freeze so the
missing coverage is intentional.

## Topic 22 — Design gaps surfaced by the test audit

**Q46 [P1] [DECIDED · ADR-0011 H3 · lean A accepted] — Introduce an injected RNG/seed seam for deterministic backoff
jitter?** V2 asserts backoff is "deterministic under the test clock," but the manual
clock controls time, not the RNG that produces jitter (TESTING.md finding K). Without a
seeded/injected RNG (a sibling of the clock seam) the schedule is not reproducible and
the unit test can assert bounds but not values. **Lean: add an injected RNG seam**
alongside the clock in the `testing/` fixtures (real RNG by default, seeded in tests);
likely a one-paragraph addition to ADR-0011 or a small new ADR.

**Q47 [P1] [DECIDED · ADR-0020 · lean A accepted] — What are the failure semantics of `map`/`gather` and child
workflows?** V4 specifies the happy-path fan-out but not what happens when an item, a
`gather` member, or a child workflow *fails* (TESTING.md finding, V4). Questions: does
one failed item fail the whole `map`/`gather` (asyncio.gather default) or are
results/exceptions collected? How does a failed child surface to the parent's durable
call — does it raise, or record a failed hit the parent re-raises on replay? **Lean:
fail-fast by default** (a failed item/member/child raises through the composite, matching
native `await` semantics and ADR's "native exceptions" promise), with collect-style
behavior deferred post-MVP. Needs confirming and probably an ADR, plus tests in V4.

**Q48 [P2] [DECIDED · ADR-0021 · lean A accepted] — Resolve the `wait_for_event(timeout=)` race and multi-event
ordering.** Undefined today (TESTING.md finding, V3): when a matching event and the
timeout are both pending, which wins; and when several buffered inbox events match one
`(type, key)`, which is consumed. **Lean: event wins over a simultaneously-due timeout**
(deliver-then-timeout, checked in that order in the poll loop), and **FIFO by
`received_at`** for multiple matches. Confirm and add V3 tests for both.

**Q49 [P1] [DECIDED · ADR-0004 + ADR-0014 H3 · lean A accepted] — Pin the exact spill threshold, and confirm redaction applies to
spilled blobs.** V8's boundary unit test needs an exact byte count, not "~256 KB /
roughly" (TESTING.md finding, V8); and no test confirms a secret in a spilled payload is
still redacted on read (finding I). **Lean: pin an exact threshold** (e.g. 262144 bytes
= 256 KiB on the encoded payload) so the boundary test is writable, and **apply the
redactor after blob rehydration** so spilled and inline payloads are redacted
identically — with a V8 test for a sensitive field in a spilled output.

---

### H3 write-up complete (2026-07-22)

All test-audit questions (Q40–Q51) resolved; Jian accepted every lean. Two new ADRs:
**ADR-0020** (composite failure semantics — fail-fast for `map`/`gather`/`start_child`)
and **ADR-0021** (event delivery ordering & the `wait_for_event` timeout race).
Refinement sections added to **ADR-0011** (Q40/Q41/Q46/Q51), **ADR-0009** (Q42/Q44),
**ADR-0014** (Q43), **ADR-0018** (Q45), **ADR-0016** (Q50), and **ADR-0004** (Q49).
Rippled through CONTEXT (glossary: interruption marker, blob spill; decisions D21–D22)
and PRD (implementation decisions + testing decisions + out-of-scope). TESTING.md updated
with the resolutions. This closes step **H3**; next is **H4** (update the `## Test Plan`
section in each SLICE-V*.md to reflect these decisions).

---

## Topic 23 — Gaps surfaced while applying the audit (step H4)

> Raised 2026-07-22 while editing the slice `## Test Plan` sections (H4). Each is a gap
> the H2 audit *named* but that H3 never resolved (they fell outside the Q40–Q51 set).
> They are **OPEN** — flagged so the affected test plans do not silently encode an
> undecided behaviour. Each carries a lean for the next confirm pass.

**Q52 [P2] [DECIDED · ADR-0009 H4 · lean A accepted] — Does the interruption marker cover the V1 crash case?**
Q42 (ADR-0009, and the CONTEXT glossary) defines the ⚡ marker as a
**`WorkflowWaiting` → `WorkflowResumed`** transition, deliberately replacing the fuzzy
"gap" heuristic. But V1's crash-recovery produces a `WorkflowResumed` with **no preceding
`WorkflowWaiting`**: a hard `kill` cannot append one, and `WorkflowWaiting` is only
introduced in V3. Under the strict transition definition the headline V1 crash would
render **no** marker — contradicting V1's demo and FRAME ("the interruption and resume are
visible"). Options: **(A)** anchor the single shared read/view-layer computation on the
`WorkflowResumed` event and classify it as *graceful* (preceded by `WorkflowWaiting`, V3+)
vs *crash* (preceded by a non-graceful gap, V1) — one owner, still precise, covers both;
**(B)** on resume of an abandoned non-terminal run, append a **synthetic `WorkflowWaiting`**
before `WorkflowResumed` so the transition is uniform. **Lean: (A)** — no synthetic events,
and the marker anchors on the one event that always signals a resume.

**Resolved (H4, 2026-07-22 — lean A, writer-side form):** the worker appends
`WorkflowResumed` **only when re-driving a run that was not durably parked** (a crash /
mid-execution interruption); a graceful wake from a `WorkflowWaiting` writes none. The
read/view-layer marker is therefore the **presence of a `WorkflowResumed` event**, computed
once and consumed by the CLI and Studio. A crash *while parked* correctly shows no marker
(nothing was lost). Option B (synthesising a `WorkflowWaiting`) was rejected as writing
fiction into the append-only journal (ADR-0004). Applied to ADR-0009 (H4 refinement), the
CONTEXT glossary, V1 (event definition + build step 12 + test plan), V6 (build step 3 +
test plan), and V3 (build steps + a new "graceful wake shows no ⚡" test). MVP scope: ⚡
means crash interruption only. See the explainer artifact for the full rationale.

**Q53 [P2] [DECIDED · ADR-0004 H4 · lean A accepted] — Fork of a non-terminal (running/waiting) run.**
V7 fork semantics are specified only for a **completed** source run; forking a live run is
undefined (TESTING.md V7 planning insight: "the live-run cases need an explicit decision
before V7 implementation"). Options: **(A)** MVP forks only **terminal** runs — reject a
fork of a non-terminal run with a clear error, defer live-run fork post-MVP; **(B)** allow
it by copying the journal to the fork point regardless of source status. **Lean: (A)** —
copy-to-point is well-defined for a settled source, whereas a live source's later events
racing the fork add semantics the MVP payoff (fork a finished run under changed code) does
not need. Then V7 tests fork-of-terminal plus a **negative** test that fork-of-non-terminal
is rejected (this is how the V7 plan is written pending confirmation).

**Resolved (H4, 2026-07-22 — lean A):** the MVP forks only **terminal** runs
(`completed`/`failed`/`cancelled`); a fork of an actively-executing run is rejected with a
clear error naming the run's status. The guard is a **status allow-list** so widening it to
quiescent `waiting` runs (a safe, natural first extension) is a one-line change later.
Applied to ADR-0004 (H4 refinement) and V7 (fork design item + test plan). No new ADR.

**Q54 [P3] [DECIDED · ADR-0004 + ADR-0017 H4 · lean accepted] — Blob lifecycle: fork sharing, orphan GC, concurrent dev.**
V8 spill leaves three storage-lifecycle edges undefined (TESTING.md V8 missing): (1) does a
fork **copy or share** a referenced blob; (2) are **orphaned** blobs ever collected (e.g. on
run deletion); (3) is a **second `satay dev`** on the same `./.satay/` refused. **Lean:**
blobs are immutable/append-only (ADR-0004), so a fork **shares** the reference and the source
stays byte-for-byte unchanged (V7); **no** blob GC and **no** run-deletion in the MVP (state
out of scope); a second `satay dev` on one data dir is **refused** by the single-writer/WAL
model (ADR-0012/0017). If accepted this is one V8 "fork shares the blob" test plus stated
out-of-scope notes, not new behaviour (this is how the V8 plan is written pending confirmation).

**Resolved (H4, 2026-07-22):** (1) a fork **shares** the source's blob references (blobs are
immutable, source stays byte-for-byte unchanged); (2) **no run deletion and no blob GC** in
the MVP — blobs accumulate under `./.satay/`, manual removal is the escape hatch, and a
future retention / `satay gc` policy must be **reference-aware** because forks share blobs;
(3) a second `satay dev` on one data dir is **refused** by an exclusive data-dir lock
acquired at startup — pulled **into the MVP** (V8), not deferred, because it protects the
single-writer invariant the durability model rests on. Applied to ADR-0004 (blob sharing /
no GC), ADR-0017 (data-dir lock), V7 (fork shares spilled blobs), and V8 (startup lock +
refusal test, fork-shares-blob test, tightened out-of-scope note).
