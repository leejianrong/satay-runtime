# ADR-0038 — `CallStatus`: one enum for the per-call, per-attempt, and control-plane vocabularies

- **Status:** Accepted
- **Date:** 2026-08-28
- **Deciders:** Jian (leejianrong2@gmail.com)

Closes the gap [ADR-0033](0033-reading-a-run-without-forking.md)'s Consequences named directly:
"the codebase carries two further un-enumerated status vocabularies (attempt-level, and the
control plane's `cancelling`/`accepted`). Enumerating one in isolation would imply a consistency
that does not exist. A `CallStatus` enum belongs with a sweep of all three." Follows the
`RunStatus` precedent (KAN-524) exactly: a `StrEnum`, so every existing `== "completed"`
comparison keeps working unchanged, while `is CallStatus.X` and an exhaustive `match` become
available to new code.

## Context

Three status vocabularies existed as bare strings, with nothing tying them together:

| vocabulary | where | values seen today |
|---|---|---|
| per-call | `RecordedCall.status` (`satay.inspect`/`satay.diff`), `satay.control.views` task/map/tree nodes | `"completed"` / `"failed"` / `"running"` |
| per-attempt | `ReadAPI.task_detail`'s attempt dicts (no dataclass exists for an attempt) | same three, independently assigned |
| control plane | four `satay.control.server` write-endpoint responses | `"accepted"` / `"cancelling"`, plus a fourth spot hard-coding `"running"` |

`RecordedCall.status`'s own docstring already named this as a deliberate, temporary gap: *"these
three values are the read layer's existing per-call vocabulary... Enumerating one in isolation
would imply a consistency that does not exist yet."*

**A wrinkle surfaced while sweeping it.** A `start_child` call's status is not actually drawn from
the three-value per-call vocabulary at all: `satay.control.views._calls_with_children` (and
`_build_tree`'s child node, the same shape) sets a child call's status to
`child_record.status.value` — its own child run's full `RunStatus` — falling back to a literal
`"unknown"` when the child run record cannot be found. So `RecordedCall.status` already carried
`"waiting"` / `"cancelled"` / `"unknown"` for a child call, silently outside the three values its
own docstring claimed. This was true before this ADR; the sweep is what surfaced it.

## Decision

**1. `CallStatus(StrEnum)`, six members, defined in `satay.journal.events`** beside `RunStatus` /
`TimerKind` / `TimerStatus` (the existing home for shared, core, zero-dependency status enums):
`RUNNING`, `COMPLETED`, `FAILED` (the per-call/per-attempt vocabulary), plus `WAITING`,
`CANCELLED` (mirroring `RunStatus`, reachable only for a child call) and `UNKNOWN` (the
child-run-not-found fallback). Six, not three, because `RecordedCall.status` already needed to
accept every one of them — a narrower enum would raise on the very values a child call already
produces, which the sweep should not break.

**2. `RecordedCall.status: CallStatus`**, was `str`. Constructed at the same boundary
`RunInspection.status=RunStatus(summary["status"])` already uses: `inspect()` and `diff()`'s
internal `_side()` both now do `status=CallStatus(call["status"])`, converting the untyped
dict-JSON string to the enum exactly once, at the point a public dataclass is built from it.

**3. Per-attempt status shares `CallStatus`'s values, not a new type.** Attempts have never had a
Python dataclass — `ReadAPI.task_detail` assembles them as plain dicts, JSON-shaped all the way to
the HTTP response — and nothing here asked for one. The attempt dicts' `"status"` values are now
built from `CallStatus.RUNNING.value` / `.COMPLETED.value` / `.FAILED.value` instead of bare
string literals, so the vocabulary is the same *named* thing in both places without inventing an
`Attempt` type nobody requested.

**4. The control plane's `cancelling`/`accepted` is a separate, small, unexported enum —
deliberately not folded into `CallStatus`.** `_WriteAck` (`satay.control.server`, `ACCEPTED` /
`CANCELLING`) names what a write endpoint's synchronous response means: the command was accepted
onto the queue (write-then-poll, ADR-0012), not a call's or a run's status. Forcing it into
`CallStatus` would be exactly the inconsistent merge ADR-0033 warned against, not a sweep — an
HTTP acknowledgement and a call's outcome are different questions with different exhaustive-match
shapes. `POST /runs`'s response, which today hard-coded a fourth, unrelated `"running"` literal,
now reports the actually-correct `RunStatus.RUNNING.value` instead — it was never an
acknowledgement value at all, it is genuinely the new run's status. `_WriteAck` is not exported:
`ControlAPI` (the core, non-HTTP write facade) carries no status field at all today — this
vocabulary is purely `satay.control.server`'s HTTP-response sugar, so it stays there, same as the
module boundary already drawn for FastAPI/uvicorn.

**5. `CallStatus` is exported: `satay.CallStatus`, added to `satay.__all__` and to
`tests/unit/test_public_surface.py`'s `expected` set.** Same reasoning `RunStatus` was exported
for (KAN-524): `RecordedCall` is already public, so the type of one of its fields must be
nameable without reaching into `satay.journal.events`.

**6. Every JSON-shaped dict keeps assigning the plain `.value` explicitly at construction**, never
a bare enum member — matching the existing convention (`record.status.value` throughout
`satay.control.views`) rather than relying on `StrEnum`'s incidental JSON-encodability. The wire
format is therefore provably unchanged: every HTTP response and every raw view dict emits the same
strings as before this ADR.

## Consequences

- **No wire or behavioral change.** Every string a consumer could already compare against
  (`row["status"] == "completed"`, `attempt.status === "failed"` in Studio's TypeScript, the HTTP
  JSON contract) is untouched; only Python call sites gained a shared name and `mypy --strict`
  exhaustiveness.
- **`RecordedCall.status`'s docstring stops quietly under-claiming.** It used to say "three
  values" while a child call could already produce a fourth and fifth (plus an "unknown"
  fallback); `CallStatus`'s six members document what the field actually carries instead of
  papering over the child-call case.
- **Per-attempt status still has no dataclass.** `CallStatus` gives it a shared vocabulary without
  forcing a new public `Attempt` type into existence — that remains a separate, un-asked-for
  question, same as `RunInspection.usage`'s ADR-0035 stance on not inventing types speculatively.
- **The control plane keeps a fourth, unexported enum (`_WriteAck`).** Not a loose end: it is the
  sweep's answer for that vocabulary, deliberately kept separate and out of the public surface
  because nothing in `ControlAPI` (the Python-facing write facade) has ever carried this concept —
  it is HTTP-response sugar in `satay.control.server` alone.
- `tests/unit/test_public_surface.py`'s `expected` set gained `CallStatus`; since the test asserts
  `expected <= set(satay.__all__)`, this could not have regressed silently either way, but the
  addition means the sweep is itself enforced going forward.

## Alternatives considered

- **One enum for all three vocabularies** (`RUNNING`/`COMPLETED`/`FAILED`/`WAITING`/`CANCELLED`/
  `UNKNOWN`/`ACCEPTED`/`CANCELLING`) — rejected: an HTTP write's synchronous acknowledgement and a
  call's recorded outcome are different questions, and merging them would make an exhaustive
  `match` over either half nonsensical for a consumer who only cares about that half.
- **Keep `RecordedCall.status: str`, add `CallStatus` only as a `Literal[...]` type alias for
  documentation** — rejected: gains no `is CallStatus.X` typo-proofing and no exhaustiveness
  checking, which is the entire rationale KAN-524 established for `RunStatus` and the reason this
  sweep is worth doing at all.
- **Reuse `RunStatus` itself for `CallStatus`**, since four of its five run-level members already
  coincide in string value with what a child call can carry — rejected: a task call is never
  "waiting", and collapsing the two types would blur what an exhaustive match over either one is
  actually promising a reader.
- **Introduce a real `Attempt` dataclass now**, typed with `CallStatus`, to give the per-attempt
  vocabulary a first-class Python type — rejected as scope beyond the sweep: attempts have never
  had one, nothing here asked for one, and `CallStatus` already names the vocabulary without it.
- **Normalize a child call's status down to the three-value set** (fold `WAITING`/`CANCELLED` into
  `RUNNING`/`FAILED`, drop the `UNKNOWN` fallback) — rejected: it would be a real behavior change
  disguised as a naming sweep, discarding information (`satay.inspect`/`satay.diff` callers can
  already tell a waiting child from a running task) that nothing in this ADR needs to take away.
