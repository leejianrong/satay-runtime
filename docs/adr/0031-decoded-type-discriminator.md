# ADR-0031 — The recorded type discriminator survives decode, and is a hint, never a resolver

- **Status:** Accepted
- **Date:** 2026-08-17
- **Deciders:** Jian (leejianrong2@gmail.com)

Refines [ADR-0005](0005-serialization-and-rehydration.md), which chose annotation-driven
rehydration and rejected embedding the Python class path in the data. Interacts with
[ADR-0029](0029-write-time-redaction.md) (slot-scoped write-time redaction) and
[ADR-0004](0004-append-only-journal.md) (the fork copies a prefix back through the
recording path). Implements KAN-520; the heuristic it removes came from KAN-474.

## Context

The encoder has always written a `"type"` qualname alongside a tagged dataclass, model or
enum value:

```json
{"$satay": "dataclass", "type": "app.models.Approved", "fields": {"reason": "clean"}}
```

Nothing on the replay path could ever see it. `SQLiteStore._decode_payload` calls
`codec.decode()` on every payload it reads, and `decode()` collapsed a dataclass/model tag
to a plain field dict — so by the time `rehydrate()` ran, the one exact signal the encoder
had gone to the trouble of recording was gone. Rehydration was always working on
already-decoded data.

The visible cost was in union arm selection. KAN-474 made composite annotations
(`X | Y`, `list[X]`, `dict[str, X]`, …) rehydrate to the type the first execution
produced, and it *preferred* the recorded qualname — but because that qualname was never
present on the path that matters, arm selection needed a structural fallback: encoded
shape, then the declared field-name set (exact match, then a single covering superset).
That fallback is sound where the arms differ and useless where they do not, which is why

```python
rehydrate(decode(encode(A(1))), A | B)   # A and B declare the same fields
```

was a hard `DecodeError` while the *same value* on the still-encoded path resolved to `A`.
Two paths through one codec disagreeing about the same bytes is the actual defect; the
ambiguous union is just where it shows.

There was a second, quieter cost. `create_fork` copies a source prefix **verbatim**
through the recording path, reading each payload and appending it to the new run. Reading
decoded and re-encoding meant every forked journal permanently lost its discriminators —
so the debugger's headline feature was writing lower-fidelity journals than the runs it
forked from.

## Decision

**1. `decode()` keeps the discriminator, out of band.** A tagged dataclass or model
decodes to `codec.TaggedDict`: a `dict` **subclass** holding the same field mapping, with
`satay_kind` and `satay_type` as *attributes*. It compares, iterates, reprs and
`json.dumps`-es exactly like the plain dict it replaces, so every reader above the store —
the CLI timeline, the read API, Studio, a test's equality assertion — is unaffected, and
`decode()` stays idempotent (decoding a `TaggedDict` flattens it back to a plain dict,
which is ADR-0005's untyped fallback shape).

This is option 2 of the two the card offered. Option 1 — have the store hand back
*encoded* payloads and let `rehydrate()` consume the tags — is a smaller diff in the codec
and a much larger one everywhere else: `GET /runs/{id}/timeline` serialises
`dict(event.payload)` directly, so every value slot in the read API and in Studio would
start rendering as `{"$satay": …}`. Keeping the store's contract ("payloads read back
decoded") and widening what *decoded* means costs one class and changes no reader.

**2. The qualname is compared, never resolved.** Arm selection matches the recorded string
against `f"{arm.__module__}.{arm.__qualname__}"` for the arms the *annotation* already
names. Nothing imports it, nothing looks it up in a registry, and a qualname naming a type
that is not an arm simply fails to match. ADR-0005's objection stands untouched: the data
is not coupled to module layout, because a moved or renamed class degrades to a
`DecodeError`, never to a wrong type and never to an import.

**3. Re-encoding a decoded value re-emits its tag.** `encode()` recognises `TaggedDict` and
writes the tagged form back. The read-then-write path is `create_fork`, and this is what
makes a forked journal as faithful as its source.

**4. The field-name heuristic is deleted.** With the discriminator present, narrowing by
declared field names is unreachable for anything this build records. Keeping it would mean
keeping a *guess* as the answer for exactly the case where guessing is most dangerous —
two arms whose fields agree — and the codebase's rule elsewhere (KAN-474, ADR-0022,
ADR-0027) is that a loud failure beats a plausible wrong value on the recovery path. An
untagged structured payload against two or more structured arms is now a `DecodeError`
whose message says which of the two ways the discriminator went missing.

**5. A missing or masked discriminator degrades to loud, never to wrong.** The
discriminator is *preferred*; when it does not match an arm, selection falls through to
the shape test. One surviving candidate still resolves — losing the tag costs exactness,
not correctness. Two or more raises.

## Interaction with write-time redaction (ADR-0029)

The discriminator lives **inside** an `*_ref` value slot, so `redact_value_slots()` does
walk over it. Three things follow, and they were tested rather than assumed:

- **The default pattern set cannot reach it.** Redaction matches *field names*, and none
  of `$satay`, `type` or `fields` is in `DEFAULT_REDACTION_PATTERNS`. A union-typed result
  resolves to the same arm across a crash and resume with write redaction on.
- **A custom pattern set can.** `Redactor(["type"])` replaces the qualname with
  `***REDACTED***`. That resolves to no arm, so an ambiguous union raises on resume with a
  message naming redaction as the cause — it never silently picks the other arm. A masked
  `fields` entry likewise raises a `DecodeError` rather than an `AttributeError`.
- **This is not a second `error_type`.** ADR-0029 kept `TaskFailed.error` out of the value
  slots because ADR-0027 lets a workflow *branch* on `error_type`: redacting it would make
  the first pass and the replay compute different things, silently. The discriminator is
  not that hazard. It is never read by the first pass (the value is in hand), it feeds no
  branch in user code, and it cannot change durable-call identity, ordinal allocation or
  the nondeterminism schedule — all of which are structural fields the mode cannot touch.
  Redacting it can only turn an exact rehydration into a loud failure, which is precisely
  the trade ADR-0029 already accepted for a redacted value of the wrong Python type.

## Consequences

- **`rehydrate(decode(encode(A(1))), A | B)` returns an `A`** for arms with identical
  field names, and the encoded and decoded paths now agree on every value.
- **Forked journals keep their discriminators.** A fork of a run whose prefix holds a
  union-typed completion replays the same arm the source produced.
- **A journal forked *before* this change is the one regression.** Its copied prefix has
  no tags on disk — forward-only migrations (ADR-0017) do not rewrite payloads, and
  nothing else can reconstruct a qualname that was never written. Replaying such a fork
  through a two-or-more-structured-arm union now raises where the heuristic used to guess.
  Accepted: there is no released version to be compatible with (this is a `0.1.0`
  blocker), an ordinary (non-forked) journal of any age still has its tags, and the
  failure is loud and names the cause. Unions whose arms differ in shape — object versus
  array versus primitive — are unaffected either way.
- **A task whose annotation lies now fails.** `-> A | B` returning a bare dict used to be
  narrowed by field names; it raises. That is the same call the ADR makes everywhere else.
- **Enums keep the old behaviour.** `decode()` drops an enum to its raw value, and a
  scalar has nowhere to carry an attribute, so two enum arms sharing a member value stay
  ambiguous. No worse than before, and out of scope here.
- **Nothing crosses the dependency boundary.** `TaggedDict` is a stdlib `dict` subclass;
  the codec's one new import is `satay.redaction.REDACTED`, core and stdlib-only
  (ADR-0013/0016).

## Alternatives considered

- **Have the store skip `decode()` and hand back encoded payloads** (option 1 of the
  card) — rejected above: it moves the cost onto every reader that is not `rehydrate`,
  including the read API's timeline endpoint, which has no decode step of its own.
- **Keep the discriminator as a real key** (`{"$satay_type": …, **fields}`) — rejected:
  it pollutes the value every reader sees, breaks equality against a plain dict, and puts
  a matchable field name back inside a redactable slot.
- **Add the encoded payload as a second field on `Event`** — rejected: the journal read
  format is the coupling surface with sibei-flow (stdlib frozen dataclasses), and widening
  it to carry the same data twice is a poor trade for one call site.
- **Resolve the qualname by import when the arms do not match** — rejected on ADR-0005's
  original grounds, and it would let a journal name a class the annotation never did.
- **Keep the field-name heuristic as a legacy fallback** — rejected: see decision 4. It
  would be dead for everything this build writes, live only for pre-`0.1.0` forked
  journals, and its behaviour there is a guess.
