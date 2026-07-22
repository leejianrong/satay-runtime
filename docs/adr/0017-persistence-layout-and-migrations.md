# ADR-0017 — Persistence layout and migrations

- **Status:** Accepted
- **Date:** 2026-07-21
- **Deciders:** Jian (leejianrong2@gmail.com)

## Context

ADR-0004 and ADR-0012 fixed the journal and the single-writer model but not two
operational details: where the data lives on disk, and how the schema evolves across
`satay` releases. The journal is long-lived, so the SQLite schema will outlive any
single version.

## Decision

- **Default data directory: a project-local `./.satay/`** (git-ignorable), holding the
  SQLite database and the blob-spill directory. It is overridable with `--data-dir`
  (and the equivalent environment variable). No `platformdirs` dependency for the MVP.
- **Schema versioning uses SQLite `PRAGMA user_version`**, with hand-written,
  stepwise, forward-only migrations applied on open. No Alembic or ORM migration tool.
- **Version-skew policy.** On open, pending forward migrations are applied. If the
  database's `user_version` is **higher** than the running code understands, `satay`
  refuses to open it with a clear "database written by a newer satay" error rather
  than risk corruption.

## Consequences

- Zero-config local start with an obvious storage location in dev, and an explicit
  override for other setups.
- Migrations are ordinary code in the repo, testable through the primary seam
  (ADR-0011).
- Refines ADR-0004; relies on ADR-0012.

## Refinement (H4 slice application, 2026-07-22)

- **A data directory holds one live worker; a second `satay dev` on it is refused
  (Q54).** The durability model rests on a single writer (ADR-0012), but WAL does not
  itself prevent a second process from opening the same database, so two `satay dev`
  instances on one `./.satay/` would silently race the journal into corruption — the
  worst possible failure for a durability tool. `satay dev` therefore acquires an
  **exclusive OS advisory lock on a lockfile in the data directory at startup**, releases
  it on shutdown, and on contention **refuses to start** with a clear error naming the
  holding process. This guardrail is **in the MVP** (V8), not deferred, because it
  protects the core invariant the whole product promises. The lock is process-level and
  local-disk only, consistent with the platform scope (ADR-0019).
