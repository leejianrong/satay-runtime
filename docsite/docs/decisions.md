# Decisions

Satay's design choices are recorded as architecture decision records in the repository, under
[`docs/adr/`](https://github.com/leejianrong/satay-runtime/tree/main/docs/adr). Each one states
the decision, the alternatives that were considered, and what it costs.

They are the source of truth and they are not reproduced here, so this page is an index. Links
go to the file in the repository, which is where the ADR is edited and reviewed.

## Start here

If you read four, read these.

| ADR | Why it matters |
| --- | --- |
| [0001 Event-sourced replay execution model](https://github.com/leejianrong/satay-runtime/blob/main/docs/adr/0001-event-sourced-replay.md) | Why replay from the top instead of snapshotting a coroutine. Everything else follows from this. |
| [0002 Durable-call identity](https://github.com/leejianrong/satay-runtime/blob/main/docs/adr/0002-durable-call-identity.md) | Why ordinals for ordinary calls and a required `key=` for fan-out. |
| [0006 Execution guarantees, idempotency, and effect safety](https://github.com/leejianrong/satay-runtime/blob/main/docs/adr/0006-execution-guarantees.md) | At-least-once, the idempotency-key derivation, and the `effect_safety` policy. |
| [0016 Core dependency boundary and data representation](https://github.com/leejianrong/satay-runtime/blob/main/docs/adr/0016-core-dependency-boundary.md) | Why `pip install satay` pulls nothing else in, and what is not allowed in the core. |

## The execution model

| ADR | Subject |
| --- | --- |
| [0001](https://github.com/leejianrong/satay-runtime/blob/main/docs/adr/0001-event-sourced-replay.md) | Event-sourced replay execution model |
| [0002](https://github.com/leejianrong/satay-runtime/blob/main/docs/adr/0002-durable-call-identity.md) | Durable-call identity |
| [0003](https://github.com/leejianrong/satay-runtime/blob/main/docs/adr/0003-nondeterminism-detection.md) | Nondeterminism detection |
| [0004](https://github.com/leejianrong/satay-runtime/blob/main/docs/adr/0004-append-only-journal.md) | Append-only journal as the single source of truth |
| [0005](https://github.com/leejianrong/satay-runtime/blob/main/docs/adr/0005-serialization-and-rehydration.md) | Serialization and typed rehydration |
| [0006](https://github.com/leejianrong/satay-runtime/blob/main/docs/adr/0006-execution-guarantees.md) | Execution guarantees, idempotency, and effect safety |
| [0007](https://github.com/leejianrong/satay-runtime/blob/main/docs/adr/0007-runtime-and-worker-model.md) | Local-first single-process asyncio runtime |
| [0010](https://github.com/leejianrong/satay-runtime/blob/main/docs/adr/0010-code-versioning.md) | Code-version recording and mismatch policy |
| [0020](https://github.com/leejianrong/satay-runtime/blob/main/docs/adr/0020-composite-failure-semantics.md) | Failure semantics of `map`, `gather`, and child workflows |
| [0021](https://github.com/leejianrong/satay-runtime/blob/main/docs/adr/0021-event-ordering-and-timeout-race.md) | Event delivery ordering and the `wait_for_event` timeout race |
| [0022](https://github.com/leejianrong/satay-runtime/blob/main/docs/adr/0022-nondeterminism-policy-split.md) | Splitting the nondeterminism policy out of `effect_safety`, strict by default |
| [0023](https://github.com/leejianrong/satay-runtime/blob/main/docs/adr/0023-version-mismatch-policy-split.md) | Splitting the code-version mismatch policy out of `effect_safety` |
| [0028](https://github.com/leejianrong/satay-runtime/blob/main/docs/adr/0028-fork-from-code-input-override.md) | Forking from code: `before_task=` fork points and the `workflow_input=` override |

## Storage and the local surface

| ADR | Subject |
| --- | --- |
| [0008](https://github.com/leejianrong/satay-runtime/blob/main/docs/adr/0008-model-observability.md) | Model observability via self-report; no core adapters |
| [0009](https://github.com/leejianrong/satay-runtime/blob/main/docs/adr/0009-local-surfaces.md) | Local surfaces: Studio web app, control API, event polling |
| [0012](https://github.com/leejianrong/satay-runtime/blob/main/docs/adr/0012-api-cohosting-and-single-writer.md) | API co-hosting, single-writer model, and SQLite driver |
| [0014](https://github.com/leejianrong/satay-runtime/blob/main/docs/adr/0014-local-surface-security.md) | Local-surface security |
| [0017](https://github.com/leejianrong/satay-runtime/blob/main/docs/adr/0017-persistence-layout-and-migrations.md) | Persistence layout and migrations |
| [0018](https://github.com/leejianrong/satay-runtime/blob/main/docs/adr/0018-frontend-and-studio-delivery.md) | Frontend and Studio delivery specifics |
| [0024](https://github.com/leejianrong/satay-runtime/blob/main/docs/adr/0024-dev-stack-app-module-loading.md) | `satay dev` imports the user's app modules (`--app`) |

## Packaging, tooling, and process

| ADR | Subject |
| --- | --- |
| [0011](https://github.com/leejianrong/satay-runtime/blob/main/docs/adr/0011-test-strategy-and-seam.md) | Test strategy and primary seam |
| [0013](https://github.com/leejianrong/satay-runtime/blob/main/docs/adr/0013-packaging-and-frontend-stack.md) | Packaging, dependency surface, and frontend stack |
| [0015](https://github.com/leejianrong/satay-runtime/blob/main/docs/adr/0015-development-toolchain.md) | Development toolchain |
| [0016](https://github.com/leejianrong/satay-runtime/blob/main/docs/adr/0016-core-dependency-boundary.md) | Core dependency boundary and data representation |
| [0019](https://github.com/leejianrong/satay-runtime/blob/main/docs/adr/0019-platform-release-and-tooling.md) | Platform support, release, and cross-cutting tooling |

## Product direction and monetisation

Where the project is going, and how it sustains itself. Read these before proposing
roadmap work.

| ADR | Subject |
| --- | --- |
| [0025](https://github.com/leejianrong/satay-runtime/blob/main/docs/adr/0025-positioning-agents-first.md) | The debugger is the wedge; agents first, platform second |
| [0026](https://github.com/leejianrong/satay-runtime/blob/main/docs/adr/0026-license-and-hosted-journal-plane.md) | Apache-2.0 core plus a hosted journal plane; write-time redaction |

## Other repository documents

The rest of `docs/` is planning material rather than user documentation, and it describes the
*intended* system. Where it disagrees with the code, the code is right.

- [`docs/ARCHITECTURE.md`](https://github.com/leejianrong/satay-runtime/blob/main/docs/ARCHITECTURE.md) for structure and the system model
- [`docs/CONTEXT.md`](https://github.com/leejianrong/satay-runtime/blob/main/docs/CONTEXT.md) for the glossary and the decision register
- [`docs/PRD.md`](https://github.com/leejianrong/satay-runtime/blob/main/docs/PRD.md) and [`docs/REQS.md`](https://github.com/leejianrong/satay-runtime/blob/main/docs/REQS.md) for the product framing and requirements
- [`docs/TESTING.md`](https://github.com/leejianrong/satay-runtime/blob/main/docs/TESTING.md) for the test strategy in practice
- [`docs/RELEASING.md`](https://github.com/leejianrong/satay-runtime/blob/main/docs/RELEASING.md) for how a version reaches PyPI
- [`CLAUDE.md`](https://github.com/leejianrong/satay-runtime/blob/main/CLAUDE.md) for the agent brief, which states honestly what is built
