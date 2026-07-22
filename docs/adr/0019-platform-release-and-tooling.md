# ADR-0019 — Platform support, release, and cross-cutting tooling

- **Status:** Accepted
- **Date:** 2026-07-21
- **Deciders:** Jian (leejianrong2@gmail.com)

## Context

The supported operating-system and Python matrix, the PyPI release mechanism, and a
few runtime cross-cutting choices (logging, retry implementation, coverage) were left
unspecified after ADR-0015.

## Decision

- **Platforms.** Linux and macOS are first-class; Windows is best-effort. SQLite is
  used on **local disk only**; network filesystems are unsupported and documented (a
  WAL limitation). Tested on **Python 3.12 and 3.13** in CI.
- **Release.** Publish to PyPI from GitHub Actions using **OIDC trusted publishing**
  (no long-lived tokens). The sdist and wheel are built with the CI-built Studio bundle
  vendored into the `satay[studio]` wheel (ADR-0013).
- **Logging.** Stdlib `logging` under a `satay` logger; no structured-logging
  dependency in the core.
- **Retry/backoff.** Hand-rolled and driven by the injected clock; no `tenacity`. This
  keeps the core lean and the timing testable (ADR-0006).
- **Coverage and property testing.** `pytest-cov` in CI; `hypothesis` is an optional
  dev dependency for the codec and idempotency-key derivation.

## Consequences

- A clear support boundary and a token-less release path.
- No new core runtime dependencies.
- Refines ADR-0015; relies on ADR-0006 and ADR-0013.
