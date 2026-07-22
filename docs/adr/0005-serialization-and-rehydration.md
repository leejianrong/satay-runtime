# ADR-0005 — Serialization and typed rehydration

- **Status:** Accepted
- **Date:** 2026-07-20
- **Deciders:** Jian (leejianrong2@gmail.com)

## Context

All durable boundaries (task inputs/outputs, events, workflow input/output) must
be persisted and later restored. Options for restoring a task's typed result on
replay: return a plain dict (loses the original type), embed the Python class path
in the data (couples to module layout, brittle), or reconstruct from the
declared type. Implicit `pickle` persistence is an explicit non-goal for security,
inspectability, SQL-queryability, and future TypeScript interop.

## Decision

Durable boundaries are **JSON-compatible by default**: primitives, lists, dicts,
dataclasses, TypedDicts, Pydantic models, enums, datetimes/timedeltas via tagged
representations, and explicit file/binary references. **No implicit pickle.**

On replay, a stored result is **rehydrated using the task's return type
annotation** — Pydantic `model_validate`, dataclass reconstruction, etc. — falling
back to a plain dict when the return is unannotated. A custom codec registry may
be added later for non-standard types.

## Consequences

- Typed replay requires **annotated task return types**; this is documented and
  aligns with the "type hints everywhere" principle. Unannotated tasks still work,
  returning dicts.
- Durable data is inspectable, SQL-queryable, and not coupled to Python module
  paths.
- Non-JSON-native values need an explicit tagged form or a (future) codec.
