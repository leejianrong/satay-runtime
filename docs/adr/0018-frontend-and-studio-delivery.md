# ADR-0018 — Frontend and Studio delivery specifics

- **Status:** Accepted
- **Date:** 2026-07-21
- **Deciders:** Jian (leejianrong2@gmail.com)

## Context

ADR-0013 chose Svelte + Vite for Studio but did not pin versions, styling, routing, or
how Studio stays current with a running worker.

## Decision

- **Svelte 5 (runes), built with Vite, in TypeScript.** Frontend package manager is
  **pnpm**, with a pinned **Node LTS** used in CI to build the bundle (never at
  `pip install`, per ADR-0013).
- **Styling: plain CSS (or CSS modules).** No utility-CSS framework (Tailwind) for a
  four-view app. **Routing: minimal client-side routing or conditional view
  rendering**, no heavyweight router.
- **Studio liveness: polling.** Studio polls the read API on an interval for the MVP,
  mirroring the worker's poll model; SSE/WebSocket push is deferred behind the same API
  (ADR-0009).
- **API contract.** FastAPI still emits OpenAPI, but the JSON API is **not formally
  versioned** in the MVP, because the server and Studio ship together in one extra. A
  version handshake can be added if they are ever decoupled.

## Consequences

- A small, dependency-light frontend; the build stays confined to CI.
- Studio freshness is bounded by the poll interval, acceptable local-first.
- Refines ADR-0013; relies on ADR-0009.

## Refinement (H3 test audit, 2026-07-22)

- **The JSON read-API contract is additive, not frozen (Q45).** V5 defines the contract,
  but later slices extend it: the V2 usage slot feeds task detail, V4 tree linkage feeds
  the tree endpoint, and V7 adds a version-mismatch field and `RunForked` lineage. The
  contract is therefore declared **additive and forward-compatible**: V5 enumerates the
  fields V2/V4/V7 will add, and V6 view tests assert on the fields they need while
  tolerating extra/unknown ones, so they do not break when V7 lands. (Still unversioned,
  as above, since server and Studio ship together.)
