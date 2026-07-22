# ADR-0015 — Development toolchain

- **Status:** Accepted
- **Date:** 2026-07-21
- **Deciders:** Jian (leejianrong2@gmail.com)

## Context

Several toolchain choices sat as "Proposed" in ARCHITECTURE §12. Confirming them
removes ambiguity and fixes the quality gates for the project and its CI.

## Decision

- **Environment and dependencies:** `uv`.
- **Build backend:** `hatchling` (with the Studio-bundle handling from ADR-0013).
- **Lint and format:** Ruff.
- **Type checking:** mypy in strict mode over `src/satay`.
- **Python tests:** pytest with `pytest-asyncio`.
- **Frontend tests:** Vitest, kept light because Studio's behaviour is chiefly
  verified through the JSON API (ADR-0011).
- **Code-version fallback chain** (refines ADR-0010): the git binary if a repository
  is present, else a source hash. **`dulwich` is dropped**; it would earn a
  dependency only if git internals were needed without the binary, which is not
  expected in dev.
- **CI (GitHub Actions):** lint, type-check, and the test suites on the supported
  Python versions; the Studio bundle is built here and vendored for packaging.

## Consequences

- One documented toolchain, so contributors and CI agree.
- One fewer dependency, having dropped `dulwich`.
- mypy strict raises the type bar across the core from the start.
