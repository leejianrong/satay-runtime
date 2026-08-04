# ADR-0026 — Apache-2.0 core plus a hosted journal plane; redaction moves to write time

- **Status:** Accepted
- **Date:** 2026-08-05
- **Deciders:** Jian (leejianrong2@gmail.com)

Records the licence and monetisation decision, which had no ADR. Extends
[ADR-0014](0014-local-surface-security.md) (local-surface security) and
[ADR-0009](0009-local-surfaces.md) (the read API and redactor) with a write-time
redaction requirement. Depends on the sequencing in
[ADR-0025](0025-positioning-agents-first.md).

## Context

The register carries `D-license` (Apache-2.0) but no decision about how the project
sustains itself, and hosting is now on the roadmap. That combination has to be
decided together, because the licence determines whether hosting is the *only* paid
thing or one of several, and that in turn determines whether the roadmap is under
pressure to hold capabilities back.

Satay's architecture gives it a **graduated** hosting story that is unusually cheap
at the first step:

| Tier | What it hosts | Customer credentials we would hold |
|---|---|---|
| **1** | journal ingest, retention, hosted Studio, team sharing, cost reporting | **none** — workflows run on their infra, only the journal travels |
| 2 | the control plane (`start` / `cancel` / `send_event` / `fork`) | none |
| 3 | execution itself | their model keys, their database credentials, their code |

Tier 1 needs nothing from the customer's systems, which is a materially different
proposition from anything the sibling project sibei-flow can offer: its equivalent
step requires warehouse read credentials per tenant unless repair execution stays on
the customer's runner, which is what its own ADR-0014 (`R6.1 constrains hosting`)
mandates. LangSmith has already established that developers pay for
roughly tier 1, and it is the natural upsell to the debugger wedge: if
fork-and-compare is why somebody installs Satay, "and your team can see it, and it is
retained" is why they pay.

There is one problem with shipping tier 1 as the code stands. The `Redactor` is
forced on every **read** (ADR-0009), which is correct for a local debugger and wrong
for a hosted plane: unredacted prompts, task inputs and business data still land in
the store, so the operator becomes their custodian regardless of what the read path
filters.

## Decision

**1. Licence: Apache-2.0 for the runtime and Studio, permanently.** No capability is
withheld from a self-hosted user. Multi-worker, the PostgreSQL backend, blob GC and
everything else on the platform roadmap ship in the open core.

**2. Monetisation: a hosted plane, tier 1 only in scope.** Journal ingest,
retention, hosted Studio, team sharing, and cost reporting. **Not hosted execution**
(tier 3), which is where the credential surface starts.

**3. Timing: after Satay's own `0.1.0` launch.** No hosting implementation before
then, with one exception in decision 4. Hosting is an uptime obligation, and uptime
does not yield to more build capacity, so it is worth doing once and late.

**4. Redaction moves to write time.** A write-time redaction mode must exist
**before any journal leaves a process for an external store.** Read-time redaction
stays for the local case and remains the default. This is the one hosting decision
that has to be made early, because it is cheap to design now and painful to retrofit
once journals are in flight.

**5. The plane's tenancy model stays free of Satay-specific assumptions**, so a
second product can sit on it. **sibei-flow is the designated second tenant**, and
its R6.1-preserving hosted shape is precisely tier 1 plus a webhook receiver and a
PR opener. One plane, two entitlements, one on-call rotation.

**6. The journal ingest contract is versioned.** ADR-0018 leaves the local Studio
OpenAPI unversioned, which is right for a bundle shipped with the process it talks
to. An ingest contract crossing a network boundary between independently-deployed
versions cannot be unversioned.

## Consequences

- **No open-core pressure on the roadmap.** The main hazard of open-core is the
  temptation to hold back exactly the capabilities the platform phase needs to be
  adopted (multi-worker, Postgres). Apache-2.0-forever removes that, at the cost of a
  weaker self-hosted upsell than sibei-flow assumes for itself in its ADR-0010. The
  two projects deliberately differ here, and that is fine: they have different buyers.
- **Write-time redaction is a real design task**, not a flag. It has to decide what
  is redactable at record time without breaking replay, since a replayed call must
  still match the journal. Expect it to be a mode on the recording path, with the
  redacted form being what the run actually resumes against.
- Tier 2 and tier 3 stay open as later decisions. Tier 3 needs its own ADR and
  inherits a compliance surface Satay currently has none of.
- **The plane must not encode Satay's own schema assumptions.** If it does, the
  sibei-flow tenancy in decision 5 becomes a second plane, which is the outcome this
  ADR exists to prevent. Treat "could a non-Satay producer send us a journal?" as the
  design test.
- If Satay's own launch does not land, decisions 2 and 5 invert cleanly: the plane
  becomes sibei-flow's, and nothing built under decision 4 is wasted. Keeping that
  exit cheap is the reason for decision 5.

## Alternatives considered

- **Open-core, matching sibei-flow's ADR-0010** — rejected: it would put multi-worker
  and the Postgres backend behind a licence, and those are exactly the capabilities
  that have to be freely adoptable for the platform phase to happen at all.
- **Hosted execution (tier 3) first** — rejected: it inherits model keys and database
  credentials per tenant, which is the credential surface Satay uniquely does not
  have today. Tier 1 is the only step that is free of it.
- **Pure OSS with no hosting** — rejected: it leaves the natural upsell to the
  debugger wedge unclaimed, and hosting is the one thing a local-first runtime cannot
  give a team that wants shared history.
- **Read-time redaction is good enough for hosting** — rejected: it protects the API
  response, not the store, and the store is what an operator is liable for.
