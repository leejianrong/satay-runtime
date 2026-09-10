# Journal Ingest

Satay runs on your machine and keeps its journal in a local `./.satay/`. The hosted product
keeps a **redacted, replayable copy** of that journal so a team can share run history and gate
on it. The producer side of that — turning a local journal into shipments and sending them —
is `satay.ingest`, installed with the `satay[ingest]` extra.

This page is the reference for the wire contract. It is deliberately small: the shipment is the
local journal projected onto a network envelope, nothing more.

```python
from satay.ingest import ship_run
from satay.ingest.http import HttpTransport   # needs satay[ingest]

async with HttpTransport("https://plane.example", token="…") as transport:
    await ship_run(run_id, store=store, transport=transport, producer="satay/0.1.0", tenant="acme")
```

For a runnable end-to-end walkthrough against an in-process fake plane — no network, no account —
see [`examples/ingest_shipping_demo.py`](https://github.com/leejianrong/satay-runtime/blob/main/examples/ingest_shipping_demo.py).

## The shipment

A shipment is one run's header plus an ordered slice of its events:

```json
{
  "contract_version": "1.0.0",
  "producer": "satay/0.1.0",
  "tenant": "acme",
  "redaction": "on",
  "run": {
    "run_id": "…",
    "workflow_name": "assemble_briefing",
    "status": "completed",
    "code_version": "git:…",
    "created_at": "2026-09-10T12:00:00+00:00",
    "lineage": { "source_run_id": "…", "fork_point_seq": 5 }
  },
  "events": [
    { "seq": 1, "event_id": "…", "type": "WorkflowCreated", "ts": "…", "payload": { … } }
  ]
}
```

`lineage` is present only for a forked run. The local idempotency key is **not** on the wire —
it is local-start dedup metadata and never crosses the boundary.

## Opaque `type` and `payload`

The plane treats each event's **`type` as an opaque string** and **`payload` as an opaque
object**. It stores them and serves them back byte-for-byte, and indexes only the structural
fields it owns — `tenant`, `run_id`, `seq`, `ts`, `event_id`, `status`, and `workflow_name` as a
free-text label. It never branches on an event type or reads inside a payload.

That is the whole of the contract's schema-agnosticism, and it is a hard requirement, not a
convenience: a producer that is **not** Satay ships the same envelope with its own `producer`
string and its own event `type` vocabulary, and the plane stores and serves it with no code path
that knows what a Satay `TaskCompleted` is. Satay's own read views (`inspect`, `diff`) run on the
producer or against a *served copy*, where the schema is known again — never inside the plane.

## Blobs travel out of band

A value over 256 KiB spills to a **content-addressed blob** locally, and the journal keeps only a
reference (ADR-0004). On the wire the reference rides inside the opaque payload; the bytes travel
separately, on a content-addressed blob channel. The shipper uploads a referenced blob the plane
lacks (`HEAD` then `PUT`) **before** the event that names it, and dedupes by content id — so a
blob shared across a run and its forks uploads once. Keeping bytes off the event stream keeps a
shipment small and lets the plane dedupe across a run and its forks.

## Versioned, and negotiated per shipment

`contract_version` is a semver stamped on every shipment. It is the **envelope** version, and is
deliberately distinct from the producer's own `code_version` (opaque run metadata) and from the
local store's schema version (which never crosses the wire). Additive fields are a minor bump that
old planes ignore; a field's meaning changing or a new required field is a major bump. A plane
supports at least the current and previous major, so a customer process is never forced to upgrade
in lockstep with the vendor.

## Redaction is a precondition, not a scan

Write-time redaction ([off by default](fork.md)) is the authoritative form of a journal for
shipping. A **custodial** endpoint — one whose store the vendor operates — requires
`redaction: "on"`, and the shipper **refuses to ship an unredacted journal to it**, before any
byte leaves the process:

```python
from satay.ingest import RedactionRequiredError
```

The plane records the assertion against the tenant; it never inspects payloads to verify, because
it cannot read them (they are opaque) and must not become a content scanner. Redacting is the
producer's job — which a Satay producer gets for free by enabling write-time redaction, and a
non-Satay producer must reproduce before shipping.

## At-least-once, and incremental

Delivery is at-least-once and safe to retry. An event is identified by `(tenant, run_id, seq)`
with a stable `event_id`; re-shipping a stored event is a no-op, and re-shipping a prefix after a
crash converges. The plane acknowledges the highest **contiguous** `seq` it has durably stored;
the shipper sends only events past that acknowledgement, so a long-running or parked run ships as
it advances and a crashed producer resumes from where the plane got to. Under backpressure the
plane answers `429` with `Retry-After`, and the shipper waits it out with a bounded backoff.

## What the plane cannot do

The plane holds *history*, not your workflow code, which never leaves your process. So the hosted
product serves **history and comparison of history** — and counterfactual replay (fork a run,
re-run it against a change, diff the result) runs where the code is, locally. See
[Forking a Run](fork.md) and [Reading a run](inspect.md) for that half.
