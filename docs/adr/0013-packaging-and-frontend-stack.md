# ADR-0013 — Packaging, dependency surface, and frontend stack

- **Status:** Accepted
- **Date:** 2026-07-21
- **Deciders:** Jian (leejianrong2@gmail.com)

## Context

Satay is a library that developers `import` into their own applications, so every
dependency in the core rides into their production installs. The first-pass
architecture put **Pydantic v2** in the core codec, **FastAPI + uvicorn** in the
core, and shipped the built **Studio bundle inside the core wheel**. Three problems
follow: a heavy compiled dependency (pydantic-core) that can clash with a user's own
Pydantic; a JS single-page app shipped to every production deploy that only ever runs
`satay dev` locally; and a polyglot build, because packaging the frontend pulls a
Node/Vite step into the Python build (either committing built JS or requiring Node at
`pip install`).

## Decision

- **Lean core.** The `satay` core is standard-library first, with a near-zero
  third-party dependency surface. No hard dependency on Pydantic, FastAPI, or uvicorn.
- **Pydantic is duck-typed, not required** (refines ADR-0005). On replay, a stored
  result is rehydrated by calling `model_validate` when the declared return type
  provides it, behind an optional import; stdlib dataclasses, TypedDicts, enums, and
  tagged datetimes need no third-party dependency.
- **The debugger stack lives behind a `satay[studio]` extra**, not the core: FastAPI +
  uvicorn (the API server), Pydantic (response models), and the built Studio bundle.
- **Frontend stack: Svelte + Vite + TypeScript**, built as a plain SPA (not
  SvelteKit). The timeline and execution-tree views render with a framework-neutral
  visualization library (d3), since that rendering, not the framework, is the real
  cost. Frontend unit tests use **Vitest** (the MVP still verifies Studio through the
  JSON API per ADR-0011, so these stay light).
- **The Studio bundle is prebuilt in CI** (where Node is present) and vendored into
  the `satay[studio]` wheel as data files. It is never built at `pip install`, and the
  sdist does not require Node on the user's machine. Build backend stays **hatchling**,
  with a build hook (or a prebuilt-asset check) for the vendored bundle.

## Consequences

- `pip install satay` yields a lean runtime with a tiny dependency footprint;
  `pip install satay[studio]` adds the debugger.
- Applications that embed Satay do not ship a JS SPA to production.
- The polyglot build is confined to CI, so wheels are reproducible.
- A later frontend change, or a TUI, stays cheap because Studio is a pure consumer of
  the JSON API (ADR-0009, ADR-0011).
- Updates the first-pass ARCHITECTURE §1/§3.6/§3.7/§6 and the §12 rows for
  serialization deps, HTTP framework, frontend, and frontend packaging.
