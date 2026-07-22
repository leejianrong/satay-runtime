# ADR-0002 — Durable-call identity

- **Status:** Accepted
- **Date:** 2026-07-20
- **Deciders:** Jian (leejianrong2@gmail.com)

## Context

Under event-sourced replay (ADR-0001), a re-running workflow must match each
durable call to the correct journal entry. Two schemes were considered:
(A) **sequential call-site ordinal** + task-definition name — the Nth durable
call of task `T` maps to the Nth recorded entry for `T`; (B) **argument hashing**
— identity derived from `hash(task_name, args)`.

Argument hashing requires all arguments to be cheaply and stably hashable (large
payloads, floats, and dict ordering all cause trouble), collides two legitimately
distinct calls that happen to share arguments, and drifts silently when argument
shapes change. Ordinals keep normal call signatures free of framework IDs and
make "the same call" a legible, deterministic notion.

Dynamic fan-out (`satay.map`) has no stable ordinal because item count and
completion order vary, so it needs an explicit key.

## Decision

Sequential-call-site **ordinal + task-definition name** is the implicit identity
for ordinary calls. **`satay.map` / `satay.gather` require an explicit
`key=`** per item for stable replay identity independent of ordering. Framework
identifiers never appear in user task signatures. An optional explicit `id=`
escape hatch for reorder-tolerant advanced cases may be added later.

## Consequences

- Reordering or inserting durable calls shifts ordinals → detected as a
  nondeterminism / version mismatch (ADR-0003), never silently mis-resumed.
- Developers must supply a stable `key=` for every mapped item (e.g. a source id
  or URL); documented as a hard requirement of `satay.map`.
- No hashing of potentially large task arguments on the hot path.
