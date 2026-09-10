# ADR-0045 — Tier-1 journal ingest: the implementation

- **Status:** Accepted
- **Date:** 2026-09-10
- **Deciders:** Jian (leejianrong2@gmail.com)

Builds the contract [ADR-0044](0044-versioned-journal-ingest-contract.md) designed. ADR-0044
fixed the *shape* of the versioned, schema-agnostic ingest envelope and deferred five
questions ("implementation, not contract shape") to "the implementation ADR … when the
tier-1 build is funded." Tier-1 hosting is now funded (Jian's call), so this ADR answers
those five questions, settles where the code lives against the core-dependency boundary,
and sequences the build. It changes no runtime mechanism and holds every guardrail
ADR-0044 inherited: [ADR-0013/0016](0016-core-dependency-boundary.md) (core deps), ADR-0041 §4
(no paywall on the core), [ADR-0029](0029-write-time-redaction.md) (redaction is the
producer's job), and [ADR-0032](0032-products-on-top-pipeline-graph-builder.md)'s test —
the runtime stays releasable with all hosting code deleted.

## Context

ADR-0044 established the envelope: a shipment is `{contract_version, producer, tenant,
run, events[]}`, the plane treats `type` as an opaque string and `payload` as an opaque
object, blobs travel out of band by content id, and ingest is idempotent and append-only
on `(tenant, run_id, seq)`. What it deliberately left open — because they are transport
and deployment concerns, not contract concerns — were: transport/framing, the auth and
tenant handshake, the blob channel, delivery semantics, and backpressure/quota.

Three constraints shape every answer below:

1. **The core-dependency boundary is not negotiable** (ADR-0013/0016). No `fastapi`,
   `uvicorn`, `pydantic`, `typer`, or `click` may enter the runtime core, and
   `tests/integration/test_import_hygiene.py` enforces it. A hosting server is FastAPI/
   uvicorn by nature, so it cannot live in `satay/`.
2. **The runtime must stay releasable with all hosting code deleted** (ADR-0032, applied
   to hosting as ADR-0042 applied it to eval). This is the literal test the design must
   pass: delete every ingest artefact and `make ci` is still green.
3. **A non-Satay producer must be able to ship** (ADR-0026 §5). sibei-flow is the
   designated second tenant; the design test is *could a producer that is not Satay send
   us a journal?* Everything below keeps the wire format producer-neutral.

The local mechanism ADR-0044 projects onto the wire already exists and is stable: every
event is the `satay.journal.events.Event` envelope (`run_id`, `type`, opaque `payload`,
`ts`, `event_id`, `seq`), a run carries a `RunRecord` header (`run_id`, `workflow_name`,
`status`, `code_version`, `created_at`, `idempotency_key`), a spilled value is a
content-addressed blob reference `{"$satay": "blobref", "id": <sha256>, "size": <bytes>}`
(ADR-0004), and write-time redaction (ADR-0029) rewrites only the `*_ref` value slots.
The store already exposes the reads a shipper needs — `read_events(run_id)`,
`get_run(run_id)`, `list_runs()`.

## Decision

**1. The code splits three ways along the boundary, and only the pure part is core.**

- **Envelope projection — core, stdlib only.** A pure transform from `Event` / `RunRecord`
  to the ADR-0044 shipment dicts (and back, for a served copy). No network, no new
  dependency, no I/O. It is "the local read format serialised for the wire" (ADR-0044
  consequence), so it belongs next to the journal codec, and it is deletable: nothing in
  the core imports it. It adds **no** public top-level surface — it is reached by the
  shipper, not re-exported from `satay/__init__.py`.
- **Producer shipper — an in-tree, deletable `satay[ingest]` extra.** It reads a run's raw
  events and blobs through the existing store seam, projects them, and ships them over the
  network (transport, auth, retry, backpressure). It lives in the repo so it can read raw
  `Event` envelopes without widening the public API, but behind an **optional extra** whose
  third-party deps (an HTTP client) never load for a core user, exactly as `satay[studio]`
  gates FastAPI today. `make ci` with the extra uninstalled stays green; the import-hygiene
  test is extended to prove the core still pulls in none of the shipper's deps.
- **The plane — a separate repo.** Receive, store, serve, blob store, tenancy, quota:
  FastAPI/uvicorn hosting infrastructure, kept out of `satay` entirely, exactly as
  ADR-0032 keeps the pipeline-graph builder out. It depends on the published contract
  (this ADR + ADR-0044), never on `satay` internals. This ADR does not design its storage
  engine or deployment — those are the plane repo's own ADRs.

**2. Transport and framing: HTTP over TLS, JSON header, NDJSON event stream.** A shipment
posts the `{contract_version, producer, tenant, run}` header as a JSON object, then streams
events as newline-delimited JSON — one event object per line, in `seq` order, gzip-encoded.
This is the lowest-friction thing a non-Satay producer can emit (a `for` loop and
`json.dumps`), needs no schema compiler, and keeps a shipment small and streamable so a
long-running or parked run ships incrementally (ADR-0044 decision 6). gRPC is rejected: it
buys nothing the envelope needs and raises the bar for a foreign producer. Because the
envelope is transport-agnostic by construction (ADR-0044 decision 2), this choice is
reversible without touching the contract.

**3. Authentication and the tenant handshake: a per-tenant bearer token, tenant resolved
server-side.** The producer presents a bearer API key over TLS; the plane maps the key to
a `tenant` and the set of `producer` labels it may assert, and rejects a shipment whose
body `tenant` disagrees with the key's binding. The `tenant`/`producer` in the body
(ADR-0044 decision 5) are labels the plane keys retention and quota on, **not** an identity
claim the plane trusts from the body — the key is the identity. Token issuance, rotation,
and the org/tenant model are the plane repo's concern; the contract only requires that
`tenant` is proven, not asserted.

**4. The blob channel: content-addressed, dedup-checked, out of band.** Blobs move on a
separate endpoint keyed by the SHA-256 already in the blob reference:

- `HEAD /blobs/{sha256}` → does the plane already hold this content? The producer checks
  before uploading, so a blob shared across a run and its forks (ADR-0004: forks re-derive
  the same id) uploads once.
- `PUT /blobs/{sha256}` → upload the bytes for a miss; the plane verifies the content hashes
  to the claimed id before storing (content-addressing is self-verifying and cheap).

The **sideband threshold is the existing 256 KiB spill threshold** (`SPILL_THRESHOLD_BYTES`):
a value that spilled to a local blob ships out of band, a value that stayed inline rides in
the payload. The plane refcounts blobs per tenant so a run and its forks share storage, which
is where ADR-0037's reference-aware retention meets the hosted store — the plane's retention
must be reference-aware for the same reason the local GC must be.

**5. Delivery is at-least-once with a contiguous-seq acknowledgement.** ADR-0044 decision 6
makes re-shipping safe: an event is identified by `(tenant, run_id, seq)` with a stable
`event_id`, re-shipping a stored event is a no-op, and a divergent `event_id` at an existing
`(run_id, seq)` is rejected as a producer bug. On top of that invariant:

- The plane's response to a shipment ACKs the **highest contiguous `seq`** it has durably
  stored for the run. A gap is never ACKed past.
- After a crash the producer resumes from `ack + 1`; re-sending the tail converges. This is
  the same append-and-converge an append-only journal already is (ADR-0004), so no
  reconciliation logic is needed on either side.
- Exactly-once acknowledgement is **not** built. It is additive over this invariant
  (ADR-0044 decision 6) and can land later if a tenant needs it; at-least-once is correct
  and cheaper for tier-1.

**6. Backpressure and quota are per-tenant, over standard HTTP flow control.** The plane
enforces per-tenant ingest rate and size caps and, when a tenant exceeds them, returns
`429` with `Retry-After`. The producer honours it with the runtime's own hand-rolled
bounded backoff discipline (no `tenacity`, ADR-0016) — the shipper reuses that code rather
than growing a second retry policy. The contiguous-seq ACK (decision 5) doubles as flow
control: a producer that is not seeing its ACK advance slows down without a separate signal.

**7. Redaction is a precondition enforced at the shipper, never scanned at the plane.** The
shipment declares `redaction: "off" | "on"` (ADR-0044 decision 4, mirroring ADR-0029's
mode). The custodial tier-1 endpoint **requires `on`**, and the plane records the assertion
against the tenant without inspecting payloads — it cannot read opaque payloads and must not
become a DLP scanner (ADR-0029 non-goal). Because write-time redaction is off by default
(ADR-0029), the shipper reads the store's redaction mode and **refuses to ship an
unredacted journal to a custodial endpoint**, rather than silently sending cleartext value
slots. The residual ADR-0029 gap (a secret interpolated into an exception message or passed
as a bare positional) travels unchanged; the contract narrows what the store holds, it does
not launder the producer's mistakes.

**8. Schema-agnosticism is a stored invariant with a parity test.** The plane stores `type`
as an opaque string column and `payload` as an opaque blob, and indexes only the structural
fields it owns (`tenant`, `run_id`, `seq`, `ts`, `event_id`, `status`, and `workflow_name`
as a free-text label). No plane code path branches on an event `type` or reads inside a
`payload`. The plane repo carries a **sibei-flow parity test**: a shipment with
`producer="sibei-flow"`, foreign `type` strings, and an opaque payload round-trips through
ingest and serve byte-for-byte, with no code path that knows what a Satay `TaskCompleted`
is. That test failing is the signal the plane has grown Satay-specific coupling ADR-0026 §5
forbids.

## Consequences

- **The runtime is unchanged and stays releasable.** The only core addition is a pure
  projection function with no new dependency; the shipper is an optional extra; the plane is
  a different repository. Delete all three and `make ci` is green — the ADR-0032 test, met.
- **Sequencing.** This ADR is the design; the build follows in two independent tracks:
  - **Phase B (this repo):** envelope projection (core) + the `satay[ingest]` shipper extra
    + tests, including the extended import-hygiene assertion. No plane, nothing hosted.
  - **Phase C (separate repo):** the plane — receive/store/serve, the blob channel, auth,
    tenancy, quota — against this published contract.
- **The plane cannot power replay, by construction.** It holds history, not workflow code
  (ADR-0044 consequence). Counterfactual replay (ADR-0041's wedge) runs where the code is;
  the hosted product serves history and comparison of history. This ADR does not change that
  division of labour.
- **The tier-2 control plane reuses this discipline.** When ADR-0041 §2's control plane is
  designed it inherits this versioning, this tenancy neutrality, and this auth shape rather
  than inventing a second plane — "one plane, two entitlements" (ADR-0026 §5) holds.
- **The cheap-exit property is preserved.** Because the shipper depends only on the public
  read seam and the plane only on the published contract, "if Satay's launch does not land,
  the plane becomes sibei-flow's" stays a direct reading of the design, not a hope.

## Open questions (deferred to the plane repo's own ADRs)

These are plane-internal and do not touch the wire contract or this repo:

- **The plane's storage engine and deployment** — how ingested journals and blobs are
  persisted and served at the hosted tier, and the retention implementation that meets
  ADR-0037.
- **Token issuance, rotation, and the org/tenant model** — decision 3 requires only that
  `tenant` is proven; the identity system behind the key is the plane's.
- **An exactly-once acknowledgement protocol**, if a tenant ever needs it — additive over
  decision 5, not part of tier-1.
- **Multi-region / residency** — a tier-3 compliance concern (ADR-0041), out of scope here.

## Alternatives considered

- **Put the shipper in a separate repo too** (nothing hosting-shaped in `satay`) — rejected
  in favour of the in-tree extra: the shipper needs raw `Event` envelopes, and an in-tree
  extra reads them through the existing store seam without adding a raw-read path to the
  public API. The extra is as deletable as `satay[studio]`, so the releasability test is met
  either way; keeping it in-tree avoids widening the public surface.
- **gRPC / protobuf transport** — rejected: it raises the bar for a non-Satay producer and
  buys nothing the opaque-payload envelope needs. HTTP+NDJSON is emit-able by hand.
- **Inline blobs in the event stream** — rejected: it bloats a shipment, defeats cross-fork
  dedupe, and couples blob size to stream framing. Content-addressed sideband (decision 4) is
  what the local blob store already is.
- **Build exactly-once ingest up front** — rejected: at-least-once is correct given the
  idempotency invariant (ADR-0044 decision 6) and far cheaper; exactly-once is additive if
  ever needed.
- **Let the plane scan for secrets on ingest** — rejected again (ADR-0044 already rejected
  it): the plane cannot read opaque payloads, slot-scoping is producer-side knowledge
  (ADR-0029), and a scanner is the DLP promise ADR-0029 refuses to make. Redaction stays the
  producer's precondition (decision 7).

🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_015tD3iz2vtgeRzzZazsDHRF
