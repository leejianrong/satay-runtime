# ADR-0044 — The versioned journal-ingest contract (design)

- **Status:** Accepted
- **Date:** 2026-09-10 (proposed 2026-09-08)
- **Deciders:** Jian (leejianrong2@gmail.com)

Pays down the debt [ADR-0026](0026-license-and-hosted-journal-plane.md) §6 named ("the
journal ingest contract is versioned") and its §5 schema-agnostic-plane requirement, on the
foundation [ADR-0029](0029-write-time-redaction.md) built. Sequenced by
[ADR-0041](0041-monetisation-revisited-replay-eval-as-product.md): tier 1 (journal ingest +
retention + hosted Studio) ships first, so this contract is the first hosting artefact —
but ADR-0026 §3's timing holds, so **this ADR is design only. No ingest endpoint, no
network code, no plane lands with it.** It is a design card in the sense of
[ADR-0037](0037-reference-aware-retention-and-blob-gc-design.md): the shape is decided now,
while the exit is cheap, and built when tier 1 is funded (its own implementation ADR).

## Context

Three facts converge:

1. **The plane must not encode Satay's schema** (ADR-0026 §5). sibei-flow is the designated
   second tenant, and the whole cheap-exit property (ADR-0026 §5, ADR-0041 consequences)
   depends on a non-Satay producer being able to ship a journal. The design test is literal:
   *could a producer that is not Satay send us a journal?*
2. **The contract crosses a network boundary between independently-deployed versions**, so
   it cannot be unversioned the way the local Studio OpenAPI is (ADR-0018): the producer in
   a customer process and the plane the vendor operates will not upgrade in lockstep.
3. **Redaction has to have already happened.** ADR-0029 makes write-time redaction the
   authoritative form of a journal, and slot-scoping (`*_ref` suffix) is *Satay-specific
   knowledge* — so a non-Satay producer must redact before it ships (ADR-0029 consequence).
   The plane is a custodian, not a DLP scanner; the store it operates must never receive
   cleartext value slots.

The local journal already has the right shape to project onto a wire. Every event is the
:class:`~satay.journal.events.Event` envelope — `run_id`, `type`, an opaque `payload`
mapping, `ts`, `event_id`, `seq` (per-run monotonic) — with every value behind a `*_ref`
slot inside `payload` (ADR-0004) and everything else in `payload` structural bookkeeping.
A run carries a :class:`~satay.journal.events.RunRecord` header (`run_id`, `workflow_name`,
`status`, `code_version`, `created_at`, `idempotency_key`). That envelope is *already*
schema-stable and value-indirected; the contract is its network projection, not a new model.

## Decision

**1. Scope: one direction, append-only, ingest only.** The contract carries a journal from
a producer process to the hosted store, and nothing else. It is **not** the control plane
(tier 2 `start`/`cancel`/`send_event`/`fork` — ADR-0041 §2, its own ADR) and **not**
execution (tier 3). The plane receives, retains, and serves journals; it never runs a
workflow, so it never needs model keys or a database credential — the tier-1 property
ADR-0026 built the business on.

**2. The shipment is a run header plus an ordered event stream, over an opaque payload.**
A shipment is `{contract_version, producer, tenant, run, events[]}` where:

- `run` is the header: `run_id`, `workflow_name`, `status`, `code_version`, `created_at`,
  and an optional `lineage` (`source_run_id`, `fork_point_seq`) for a forked run.
- each event is `{seq, event_id, type, ts, payload}`.

The plane treats **`type` as an opaque string and `payload` as an opaque object**: it
stores them and serves them back byte-for-byte, and indexes only on the *structural* fields
it owns — `tenant`, `run_id`, `seq`, `ts`, `event_id`, `status`, and `workflow_name` as a
free-text label. It never branches on an event `type` or reads inside a `payload`. This is
the whole of schema-agnosticism: the producer's event vocabulary is data to the plane, not
API. Satay's own read views (`inspect`/`diff`/`compare`, ADR-0033/0034) run **on the
producer or against a served copy**, where the schema is known — not inside the plane.

**3. The contract is versioned with a semver `contract_version`, negotiated per shipment.**
The producer stamps every shipment; the plane advertises the major range it accepts.
Additive fields are a minor bump and old planes ignore unknown keys; a field's meaning
changing or a required field appearing is a major bump; the plane supports at least the
current and previous major (N and N−1) so a customer process is never forced to upgrade in
lockstep with the vendor. `contract_version` is the *envelope* version and is deliberately
distinct from the producer's own `code_version` (which is opaque run metadata) and from the
store's `PRAGMA user_version` (ADR-0017, a local-schema concern that never crosses the wire).

**4. Write-time redaction is a precondition of acceptance, asserted and recorded, not
scanned.** The shipment declares `redaction: "off" | "on"` (mirroring ADR-0029's mode).
A tier where the plane is the custodian requires `on`, and the plane records the assertion
against the tenant; it does **not** inspect payloads to verify, because it cannot read them
(decision 2) and must not become a content scanner (ADR-0029's stated non-goal). The
invariant the contract states is *no cleartext value slot crosses the boundary*, and it is
the producer's to uphold — which is exactly what ADR-0029 gives a Satay producer for free
and what a non-Satay producer must reproduce before shipping. The residual gap ADR-0029
names (a secret interpolated into an exception message, or passed as a bare positional
argument) travels unchanged; the contract narrows what the store holds, it does not launder
the producer's mistakes.

**5. Tenancy is one opaque `tenant` plus one opaque `producer`, and nothing Satay-specific.**
The plane keys retention, access, and quota on `tenant`; `producer` names the software that
generated the journal (`"satay/0.1.0"`, `"sibei-flow/…"`). Neither implies a Satay run
model. This is the ADR-0026 §5 test made concrete: sibei-flow ships the same envelope with
`producer="sibei-flow"` and its own event `type` strings, the plane stores and serves them
with no code path that knows what a Satay `TaskCompleted` is, and "one plane, two
entitlements, one on-call rotation" (ADR-0026 §5) holds.

**6. Ingest is idempotent and append-only, and a run may stream as it grows.** An event is
identified by `(tenant, run_id, seq)` and carries a stable `event_id`; re-shipping an event
already stored is a no-op, and shipping a prefix again (after a producer crash) converges.
The plane rejects a shipment that would rewrite an existing `(run_id, seq)` with a different
`event_id` — the journal is append-only on both sides (ADR-0004), and a divergent seq is a
producer bug, not a merge. A run need not arrive whole: a producer may ship `[1..k]` now and
`[k+1..]` later, so a long-running or parked run is retained as it advances.

**7. Referenced blobs travel by content id, out of band.** A value spilled past 256 KiB is a
blob reference (ADR-0004); the envelope carries the reference verbatim (it is inside an
opaque `payload`), and the actual bytes are shipped through a separate content-addressed
blob channel the implementation ADR specifies. Keeping blobs off the event stream keeps a
shipment small and lets the plane dedupe identical blobs across a run and its forks — which
is also where the reference-aware retention of ADR-0037 will eventually meet the hosted
store.

## Consequences

- **The §6 debt is designed, and nothing is hosted.** This is the contract only — no
  endpoint, no transport, no auth, no plane. It stays off the critical path (ADR-0041
  consequences): evaluation and the agent on-ramp shipped without it, and the runtime is
  unchanged and releasable, exactly as ADR-0026 §3's timing requires.
- **The cheap-exit property is preserved and, in fact, exercised by the design.** Because
  the plane is schema-agnostic and the redaction burden sits on the producer, "if Satay's
  own launch does not land, the plane becomes sibei-flow's" (ADR-0026 §5) is not a hope but
  a direct reading of decisions 2 and 5.
- **The tier-2 control-plane contract inherits this discipline.** ADR-0041 §2 pulls the
  control plane forward; when it is designed it reuses this versioning scheme and this
  tenancy neutrality rather than inventing a second one, so the plane stays one plane.
- **The local read format stays the coupling, and this is its network projection.** Satay
  and sibei-flow are "coupled only through the journal read format" (stdlib frozen
  dataclasses); the ingest envelope is that format serialised for the wire, which is why it
  must carry the same structural fields and no more.
- **The plane cannot power replay.** Replay needs the workflow code, which never leaves the
  customer (tiers 1–2). So the hosted product serves *history and comparison of history*,
  and counterfactual replay (ADR-0041's wedge) runs where the code is — the honest division
  of labour the tier boundaries already imply.

## Open questions (deferred to the implementation ADR)

These are implementation, not contract shape, and each needs the tier-1 build to be funded
before it is answered:

- **Transport and framing** — HTTP+JSON, newline-delimited streaming, or gRPC; batch size
  and compression. The contract is transport-agnostic by construction (decision 2).
- **Authentication and the tenant handshake** — how `tenant`/`producer` are proven. None of
  it changes the envelope.
- **The blob channel** — content-addressing scheme, inline-vs-sideband threshold, dedupe and
  its intersection with ADR-0037 retention on the hosted side.
- **Delivery semantics** — at-least-once ingest is assumed (decision 6 makes it safe); an
  exactly-once acknowledgement protocol, if wanted, is additive.
- **Backpressure and quota** — per-tenant ingest limits and how the producer is told to slow
  down.

## Alternatives considered

- **Ship Satay's Python event classes / a Satay-shaped schema** — rejected: it couples the
  plane to Satay's event vocabulary and breaks ADR-0026 §5, turning the sibei-flow tenancy
  into a second plane, which is the outcome ADR-0026 exists to prevent.
- **Reuse the unversioned local Studio OpenAPI (ADR-0018)** — rejected by ADR-0026 §6: an
  unversioned contract is right for a bundle shipped with the process it talks to, and wrong
  across a boundary between independently-deployed versions.
- **Have the plane interpret payloads for richer server-side indexing/search** — rejected on
  two counts: it reintroduces the schema coupling decision 2 removes, and it needs cleartext
  the plane must not hold (ADR-0029). Indexing stays on the structural fields the plane owns.
- **Require whole-run shipments** — rejected: a parked or long-running run (ADR-0030) would
  be invisible until terminal, and a producer crash mid-run would have nothing retained.
  Append-and-converge (decision 6) is what an append-only journal already is.
- **Make redaction the plane's job (scan on ingest)** — rejected: the plane cannot read
  opaque payloads, slot-scoping is producer-side knowledge (ADR-0029), and a scanner is the
  DLP promise ADR-0029 explicitly refuses to make.

🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_01Dg1niioKdtQhxpnZKvtnvP
