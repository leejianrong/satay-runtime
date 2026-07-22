# ADR-0010 — Code-version recording and mismatch policy

- **Status:** Accepted
- **Date:** 2026-07-20
- **Deciders:** Jian (leejianrong2@gmail.com)

## Context

Because workflows replay their logic (ADR-0001), resuming a run under changed
code can silently diverge. Satay must record which code produced a run and detect
mismatch honestly. Automatic migration of long-running workflows is out of scope
for the MVP; the MVP need only detect and surface mismatch.

## Decision

Every run records a **code version**, resolved by a fallback chain:
**git commit** if inside a repo, else a **developer-provided string**, else a
**content hash of the workflow source**. On resume under a different version:
**dev mode warns clearly**; **strict mode may reject** automatic resume; the user
may explicitly **fork** (ADR-0004) to continue under new code. No automatic
migration; no management of historical deployment artifacts in the MVP.

## Consequences

- A run always carries an honest version stamp with zero required configuration.
- Version-mismatch handling reuses the dev-warn / strict-reject split shared with
  nondeterminism detection (ADR-0003) and effect safety (ADR-0006), and the fork
  mechanism (ADR-0004).
- Studio surfaces code-version mismatch warnings on affected runs.
