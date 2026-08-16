# ADR-0029 — Write-time redaction: slot-scoped, off by default, redacted-is-authoritative

- **Status:** Accepted
- **Date:** 2026-08-16
- **Deciders:** Jian (leejianrong2@gmail.com)

Implements decision 4 of [ADR-0026](0026-license-and-hosted-journal-plane.md) ("a
write-time redaction mode must exist before any journal leaves a process for an external
store"), which anticipated that the design would need its own ADR. Extends
[ADR-0009](0009-local-surfaces.md) and [ADR-0014](0014-local-surface-security.md), whose
read-time redactor is unchanged and stays the default. Depends on the identity rule in
[ADR-0002](0002-durable-call-identity.md) and the payload indirection in
[ADR-0004](0004-append-only-journal.md).

## Context

The `Redactor` is forced on every **read** (N18). For a local debugger that is the right
place for it: the store never leaves the machine, and what needs protecting is the value
rendered into a browser tab. For a hosted journal plane it is the wrong place. Unredacted
prompts, task inputs and business data still land in `satay.db`, so an operator who
ingests that journal becomes their custodian regardless of what the read path filters.
Read-time redaction protects the API response; the store is what an operator is liable
for.

ADR-0026 sketched the answer as "a mode on the recording path, with the redacted form
being what the run actually resumes against", and flagged the hard part: **a replayed
durable call must still match the journal.** Nondeterminism detection is strict by
default ([ADR-0022](0022-nondeterminism-policy-split.md)), so anything that shifts what
replay matches turns a redacted journal into a `NondeterminismError` on the recovery
path — the one path that must not be the flaky one.

Two facts about the existing design decide most of this:

1. **Identity carries no user data.** A durable call's identity is `(task_name, ordinal)`,
   or `(task_name, key)` for a fan-out item (ADR-0002). The idempotency key is derived
   from `(run_id, task_name, ordinal_or_map_key)` and *deliberately excludes arguments*.
   Detection compares the durable-call **schedule**, not arguments (ADR-0003/0022).
2. **Values already live in their own slots.** ADR-0004 puts every value behind `*_ref`
   indirection — `input_ref`, `output_ref`, `event_ref`, `source_input_ref`, and the
   inbox's `payload_ref` column — so the envelope stays schema-stable. Everything else in
   a payload — `task_name`, `ordinal`, `key`, `identity`, `code_version`, `child_run_id`,
   `attempt`, `error`, timer ids — is runtime bookkeeping.

Those two facts do not overlap. That is the whole opening.

## Decision

**1. Write-time redaction is a mode on the store, off by default.**
`WriteRedaction` ∈ `off`/`on` in `satay.config`, resolved by `resolve_write_redaction()`
with override → `SATAY_WRITE_REDACTION` → `off` precedence, and accepted as
`SQLiteStore.open(..., write_redaction=..., redactor=...)`. Because every store the
runtime opens goes through `SQLiteStore.open` — `satay.start`'s project-local default,
`satay dev`'s, a test's — the environment variable is sufficient wiring; no call site
needs to learn about it. Read-time redaction is unchanged in both modes.

Two modes, not the `off`/`warn`/`strict` triple of `EffectSafety`,
`NondeterminismPolicy` and `VersionMismatchPolicy`. Those are checks that can pass or
fail. This is a choice about what gets written, and there is no third thing to do.

**2. Redaction is slot-scoped, and the scope is a naming rule rather than a list.** When
the mode is on, the store applies the redactor to every payload field whose name ends in
`_ref`, plus the inbox's `payload_ref` column, and to nothing else. Structural fields are
handed through byte-identical whatever the pattern set says. This is the replay-identity
guarantee, and it is structural rather than a promise about the default pattern list: even
a deliberately hostile pattern set (`Redactor(["key"])`, `Redactor(["name"])`) cannot reach
the fields identity is derived from, so a redacted journal resolves exactly the same calls
in exactly the same order as an unredacted one.

The scope is `is_value_slot()` — a suffix test — and not the `VALUE_REF_FIELDS` set, which
is kept only as documentation of the slots that exist today (`input_ref`, `output_ref`,
`event_ref`, `source_input_ref`). A hand-maintained list is the part that rots: a later
slice that adds an event type carrying a new `*_ref` would need someone to remember, and
the failure of forgetting is a secret in the store — silent, and visible only to whoever
ends up holding the journal. The suffix already *is* the convention ADR-0004 established
for value indirection, so following it costs nothing and closes the gap by default. A
false positive is harmless in the other direction: a structural `*_ref` holding a plain
string id has no field name inside it to match, so the redactor returns it unchanged.

**The `error` payload is deliberately not a value slot.** `TaskAttemptFailed` and
`TaskFailed` (ADR-0027) carry `{type, message, traceback}`, three runtime-generated
strings. None of those *names* can match a pattern, so including them would protect
nothing — while a custom pattern set that *did* match `type` would rewrite the
`error_type` a collect-mode workflow branches on, and that value is read back out of the
journal on replay. Redacting it would manufacture precisely the first-pass-versus-replay
divergence ADR-0027 exists to prevent. So `error` sits on the replay-load-bearing side of
the line with `task_name` and `key`, not on the value side. The residual exposure — a
secret interpolated into an exception message — is real, and is the same content-scanning
gap called out below; it is not closed by moving this field.

**3. It runs before spill, not after.** Order on the write path is encode → redact →
spill. A redacted value must never reach a blob file either, and once redacted the value
is usually too small to spill at all.

**4. The redacted form is authoritative.** The journal remains the source of truth; the
mode changes *what the truth is*. A redacted `output_ref` is what a replayed call yields.
A redacted workflow `input_ref` is what a resume or a fork re-enters the workflow with.
This is not a lossy view over a complete record — the original is genuinely gone, which
is the point.

**5. Redacting a workflow input warns, once per run.** A workflow's `input_ref` is the
only redactable slot that is also a *resume seed*: `satay.timers` and
`satay.control.commands` both rehydrate it to re-enter a parked or forked run. Every
other slot records something that already happened. So when redaction actually changes a
`WorkflowCreated` input, the store logs a warning naming the run, because past the replay
frontier the workflow body will compute from the placeholder. The remedy is a workflow
shape, not a runtime feature: fetch secrets inside a task, or pass them per-task, rather
than as workflow input.

**6. The redactor moves to the core.** `satay.redaction` is a new stdlib-only core
module; `satay.control.redaction` re-exports it. The write path is the journal store,
which is core (A3) and must not import the control package (A7/A8, nominally the
`satay[studio]` extra). Both were pure Python already, so nothing about the packaging
promise changes — but a core module reaching up into the control plane is the wrong
direction, and `import satay.journal.store` should not depend on the API surface staying
FastAPI-free by accident (ADR-0013/0016).

## Consequences

- **The hosting prerequisite in ADR-0026 decision 4 is met, and nothing is hosted.** This
  is the seam only: no ingest endpoint, no network code, no plane.
- **Replay across a redacted journal is unchanged in shape.** Identity resolution, ordinal
  allocation, fan-out keying and nondeterminism detection all read structural fields the
  mode cannot touch, so `strict` stays safe to leave on. Tests drive a crash and a resume
  against a write-redacted journal through the `FaultInjector` seam to hold this.
- **A resumed run can compute from a placeholder.** With the mode on, a workflow that
  takes a secret as *input* resumes with `***REDACTED***` in its hand and will happily
  pass it to a real API past the replay frontier. Decision 5 makes that loud rather than
  silent; it does not make it impossible, because the alternative — exempting the resume
  seed — would leave the secret in the store, which is the thing the mode exists to stop.
- **A redacted value can break typed rehydration on resume.** The placeholder is a `str`.
  If the matching field is declared as an `int`, an enum, or a `SecretStr`, the
  ADR-0005 rehydration of that value will raise on the recovery path. Accepted: a loud
  failure beats a wrong type, and it is the same trade already made for union arms that
  cannot be discriminated (KAN-474).
- **Field-name matching does not become content scanning.** A secret passed as a bare
  string (`start(wf, "sk-...")`), or embedded in an exception message that reaches a
  `WorkflowFailed` traceback, has no field name to match and survives in both modes. The
  mode narrows what the store holds; it is not a DLP scanner and must not be sold as one.
- **Still not encryption at rest.** Everything not matched is stored verbatim.
- **A fork is redacted by the mode in force when the fork is taken.** `create_fork`
  re-appends the source prefix through the same write path, so forking an already-redacted
  run is a no-op (the placeholder redacts to itself) and forking an *unredacted* run with
  the mode on yields a redacted fork. Turning the mode on never rewrites history — the
  journal is append-only, and an existing run keeps whatever it already recorded.
- **`satay.fork(..., workflow_input=...)` is redacted on the way in** (ADR-0028). The
  override is written into the fork's `WorkflowCreated` rather than passed at drive time,
  precisely so it is durable — which means it goes through the recording path and is
  scrubbed like any other input, and the resume-seed warning fires naming the *fork's* run
  id. That is the correct outcome: a fork whose new input escaped redaction would be a
  second way into the store, and "fork the run and hand it the real key" is exactly the
  thing an operator must not be able to do by accident. `RunForked`'s `source_input_ref`
  — the input the override replaced — is covered by the suffix rule for the same reason,
  and would have been missed by a hand-maintained list.
- Decision 6 in ADR-0026 (a versioned ingest contract) is untouched and still owed. So is
  the "could a non-Satay producer send us a journal?" design test — slot-scoping is
  Satay-specific knowledge, so a producer that is not Satay must redact before it ships,
  which is exactly what this mode gives it.

## Alternatives considered

- **Redact the whole event payload, not just the value slots** — rejected: it puts
  `task_name`, `ordinal` and a fan-out `key` one unlucky pattern away from being rewritten,
  and the failure mode is a `NondeterminismError` on resume, i.e. only on the recovery
  path, only for some runs. The default pattern set avoids the structural keys today, but
  a custom pattern set is a supported feature and this design must not depend on the user
  choosing well.
- **Keep the value and store a redacted copy alongside** — rejected: the operator still
  holds the secret, which is the entire objection ADR-0026 raises against read-time
  redaction. It would also double every payload.
- **Encrypt at rest instead of redacting** — rejected for now, not on the merits: it
  solves a different problem (an operator who *should* eventually see the data, protected
  from disk theft) and it needs a key-management story Satay has none of. It stays open
  as a later, complementary decision.
- **Exempt `WorkflowCreated.input_ref` so resume is always exact** — rejected: it is the
  single most likely place for a secret to appear, and exempting it would make the mode
  claim more than it delivers. A warning is the honest version.
- **Make write-time the default and keep read-time as the opt-in** — rejected: the local
  debugger is the first user (ADR-0025), and for it destroying data by default to protect
  a store that never leaves the laptop is a bad trade. Off by default, per ADR-0026.
