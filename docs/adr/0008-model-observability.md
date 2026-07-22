# ADR-0008 — Model observability via self-report; no core adapters

- **Status:** Accepted
- **Date:** 2026-07-20
- **Deciders:** Jian (leejianrong2@gmail.com)

## Context

Satay Studio's task view promises Model / Tokens / Estimated cost. But
"LangChain-scale integrations" and "automatic instrumentation of arbitrary Python
calls" are explicit non-goals. The tension: how does the metadata reach the
journal without patching provider SDKs? Options: (A) tasks **self-report** via a
context object; (B) **auto-instrument** known SDKs by monkeypatching; (C) **zero**
model surface in core, pushing all of it into the app.

Option B violates the non-goal and is per-provider and version-fragile. Option C
abandons a promised Studio feature and leaves no standard place for cost.

## Decision

- Tasks **self-report** model usage via the task context, e.g.
  `ctx.record_model_usage(model, input_tokens, output_tokens, ...)`. The journal
  stores a **generic usage/cost slot**, not a model-specific schema.
- **The core ships no model adapters.** The reference application calls a provider
  SDK directly inside its tasks and self-reports usage. This keeps "build the
  runtime before the ecosystem" honest.

## Consequences

- Provider-agnostic, tiny API surface, works for any model or non-LLM cost.
- Metadata is opt-in: a task that does not report shows no usage in Studio.
- Any future model adapters are a separate, optional library — never a core
  dependency.
