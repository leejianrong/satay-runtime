"""JSON codec with tagged types and annotation-driven rehydration (N12, ADR-0005).

JSON-compatible by default: primitives, lists, and dicts pass through unchanged.
Non-JSON-native values (datetime, timedelta, enum, dataclass) round-trip through a
tagged object of the form ``{"$satay": "<kind>", ...}``. There is **no pickle
anywhere** — an un-encodable value raises :class:`EncodeError` naming the offending
path.

On replay a stored result is rehydrated from the task's return annotation: a Pydantic
model via ``model_validate`` (duck-typed — Pydantic is *not* a core dependency), a
dataclass by field reconstruction, an enum/datetime/timedelta by its native
constructor, falling back to the decoded JSON value (typically a dict) when the
annotation is absent.

Rehydration recurses through parametrized annotations — ``list[X]``, ``tuple[X, Y]``,
``dict[str, X]``, ``X | None``, ``X | Y`` and any nesting of them — so a **resumed**
value has the same Python type as the **first-execution** value (KAN-474). Union arms
are discriminated by the ``"type"`` qualname the encoder records on tagged values, which
survives :func:`decode` on a :class:`TaggedDict` (KAN-520, ADR-0031) and so is available
on the replay path too; falling back to the encoded shape when it is absent. An
annotation whose reconstruction cannot be resolved raises :class:`DecodeError` naming
the annotation, rather than silently degrading to a plain dict on the recovery path
only, or — worse — guessing an arm.
"""

from __future__ import annotations

import dataclasses
import enum
import json
import types
import typing
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any, get_args, get_origin, get_type_hints

from satay.redaction import REDACTED

#: The discriminator key marking a tagged (non-JSON-native) value.
TAG_KEY = "$satay"

#: The tagged kinds that carry a ``"type"`` qualname discriminator.
_TYPED_KINDS = ("dataclass", "model", "enum")

#: ``type(None)``, the arm ``X | None`` adds to a union.
_NONE_TYPE = type(None)


class TaggedDict(dict[str, Any]):
    """A decoded dataclass/model value that remembers the encoder's discriminator.

    :func:`decode` collapses a tagged dataclass or model to the plain mapping of its
    fields, which is what every reader above the store wants — the CLI timeline, the read
    API, an equality assertion in a test. But the ``"type"`` qualname the encoder recorded
    is the only **exact** signal for choosing a union arm, and dropping it forced arm
    selection to guess from field names (KAN-520).

    So the decoded value is a ``dict`` *subclass*: it compares, iterates, serializes and
    reprs exactly like the plain dict it replaces, and carries the discriminator
    **out of band** — as an attribute, not a key — where no reader can trip over it, no
    ``json.dumps`` will emit it, and no field-name redaction pattern can match it.

    The qualname is a **hint that is compared, never an import target**: nothing here
    resolves it to a class, so ADR-0005's rejection of "embed the Python class path in the
    data" still holds — a renamed or moved class degrades to a loud
    :class:`DecodeError`, never to a wrong type (ADR-0031).
    """

    __slots__ = ("satay_kind", "satay_type")

    #: The tagged kind this value decoded from — ``"dataclass"`` or ``"model"``.
    satay_kind: str
    #: The recorded ``module.QualName`` of the encoded type, or ``None`` if absent.
    satay_type: str | None

    def __init__(
        self,
        fields: Mapping[str, Any],
        *,
        satay_kind: str,
        satay_type: str | None = None,
    ) -> None:
        super().__init__(fields)
        self.satay_kind = satay_kind
        self.satay_type = satay_type


class EncodeError(TypeError):
    """Raised when a value cannot be encoded to the JSON-compatible form.

    The message names the JSON path (``$.a.b[0]``) of the offending value so the
    author can see exactly what is not serializable. No pickle fallback exists.
    """


class DecodeError(ValueError):
    """Raised when a tagged object is malformed and cannot be decoded."""


def _path(base: str, key: str) -> str:
    return f"{base}.{key}"


def _index(base: str, i: int) -> str:
    return f"{base}[{i}]"


def encode(value: Any, *, _path_str: str = "$") -> Any:
    """Encode ``value`` into a JSON-compatible structure with tagged non-native types.

    Returns primitives/lists/dicts ready for :func:`json.dumps`. Raises
    :class:`EncodeError` (naming the path) for anything that is not representable.
    """
    if value is None or isinstance(value, bool | int | float | str):
        return value

    if isinstance(value, datetime):
        return {TAG_KEY: "datetime", "v": value.isoformat()}

    if isinstance(value, timedelta):
        return {TAG_KEY: "timedelta", "v": value.total_seconds()}

    if isinstance(value, enum.Enum):
        return {
            TAG_KEY: "enum",
            "type": _qualname(type(value)),
            "v": encode(value.value, _path_str=_path_str),
        }

    if isinstance(value, list | tuple):
        return [encode(item, _path_str=_index(_path_str, i)) for i, item in enumerate(value)]

    if isinstance(value, TaggedDict):
        # Re-emit the tagged form this value decoded from, so a payload that is *read and
        # written back* keeps its discriminator instead of degrading to an anonymous field
        # dict. The live case is ``create_fork``, which copies a source prefix verbatim
        # through the recording path (N15, ADR-0004); before KAN-520 every forked journal
        # lost its tags on the way through.
        return {
            TAG_KEY: value.satay_kind,
            "type": value.satay_type,
            "fields": {k: encode(v, _path_str=_path(_path_str, k)) for k, v in value.items()},
        }

    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        fields = {
            f.name: encode(getattr(value, f.name), _path_str=_path(_path_str, f.name))
            for f in dataclasses.fields(value)
        }
        return {TAG_KEY: "dataclass", "type": _qualname(type(value)), "fields": fields}

    # Duck-typed Pydantic support: never import pydantic in the core.
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        raw = dump(mode="json")
        return {
            TAG_KEY: "model",
            "type": _qualname(type(value)),
            "fields": encode(raw, _path_str=_path_str),
        }

    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise EncodeError(
                    f"non-string dict key {k!r} at {_path_str}; JSON objects require string keys"
                )
            out[k] = encode(v, _path_str=_path(_path_str, k))
        return out

    raise EncodeError(
        f"cannot encode value of type {type(value).__name__!r} at {_path_str}; "
        f"no pickle fallback exists (ADR-0005) — provide a dataclass, Pydantic model, "
        f"enum, datetime, timedelta, or JSON-native value"
    )


def decode(data: Any) -> Any:
    """Decode a JSON-compatible structure, resolving tagged non-native values.

    Dataclasses and Pydantic models decode to a :class:`TaggedDict` — a mapping of their
    fields that is indistinguishable from a plain dict to a reader, but keeps the
    encoder's ``"type"`` discriminator as an attribute so union arm selection stays exact
    on the replay path (KAN-520). Enums decode to their raw value; :func:`rehydrate`
    reconstructs the declared Python type when an annotation is supplied. Datetimes and
    timedeltas decode to their native Python objects because they are unambiguous from
    the tag alone.

    Idempotent on its own output: decoding an already-decoded value returns it unchanged
    (a :class:`TaggedDict` flattens back to a plain dict, which is the ADR-0005 fallback
    shape an untyped reader expects).
    """
    if isinstance(data, list):
        return [decode(item) for item in data]

    if isinstance(data, dict):
        tag = data.get(TAG_KEY)
        if tag is None:
            return {k: decode(v) for k, v in data.items()}
        if tag == "datetime":
            return datetime.fromisoformat(data["v"])
        if tag == "timedelta":
            return timedelta(seconds=data["v"])
        if tag == "enum":
            return decode(data["v"])
        if tag in ("dataclass", "model"):
            fields = data.get("fields")
            if not isinstance(fields, dict):
                raise DecodeError(
                    f"tagged {tag} value has a {type(fields).__name__!r} 'fields' entry "
                    f"instead of an object; the payload is corrupt, or a redaction pattern "
                    f"matched the 'fields' key and masked the whole value"
                )
            return TaggedDict(
                {k: decode(v) for k, v in fields.items()},
                satay_kind=tag,
                satay_type=data.get("type"),
            )
        raise DecodeError(f"unknown tagged kind {tag!r}")

    return data


def to_json(value: Any) -> str:
    """Encode ``value`` and serialize it to a JSON string."""
    return json.dumps(encode(value), separators=(",", ":"))


def from_json(text: str) -> Any:
    """Parse a JSON string and decode its tagged values."""
    return decode(json.loads(text))


def rehydrate(data: Any, annotation: Any) -> Any:
    """Reconstruct a typed value from decoded JSON using a return ``annotation``.

    - No annotation (``None``/``inspect.Signature.empty``/``Any``): return the decoded
      value (a dict for structured results) — the ADR-0005 fallback.
    - Pydantic model (has ``model_validate``): validate the raw JSON dict.
    - Dataclass: reconstruct from field values, recursing on nested annotations.
    - Enum: look up by value.
    - datetime/timedelta: already decoded natively; returned as-is.
    - Parametrized generic (``list[X]``, ``dict[str, X]``, ``X | None``, ...): recurse
      into the element/value/arm annotations (see :func:`_rehydrate_generic`).
    - A recorded ``None``: returned as ``None`` whatever the annotation says.
    """
    if data is None:
        # A recorded None is the truth even when the annotation does not admit one: the
        # first execution returned None, so handing back None is what keeps the type the
        # same across replay. Applies at every depth — a None element of a list[X], a
        # None value in a dict[str, X], a None field of a dataclass — because an
        # over-promising annotation must never become a resume-only crash (KAN-474).
        return None

    if annotation is None or annotation is Any or _is_empty(annotation):
        return decode(data)

    origin = get_origin(annotation)
    if origin is not None:
        # Generic alias (list[X], dict[str, X], X | None, ...): recurse into the
        # element/value/arm annotations so nested types are reconstructed too.
        return _rehydrate_generic(data, annotation, origin)

    if not isinstance(annotation, type):
        return decode(data)

    if issubclass(annotation, datetime | timedelta):
        return decode(data)

    if issubclass(annotation, enum.Enum):
        raw = decode(data)
        return annotation(raw)

    # Duck-typed Pydantic: reconstruct via model_validate on the raw JSON dict.
    validate = getattr(annotation, "model_validate", None)
    if callable(validate):
        raw = _raw_fields(data)
        return validate(raw)

    if dataclasses.is_dataclass(annotation):
        # Fields are recursed over *still encoded*, so nested tags (and therefore union
        # discrimination) survive into the recursive rehydrate call.
        raw = _encoded_fields(data)
        hints = get_type_hints(annotation)
        kwargs = {}
        for f in dataclasses.fields(annotation):
            if f.name in raw:
                kwargs[f.name] = rehydrate(raw[f.name], hints.get(f.name))
        return annotation(**kwargs)

    # Primitive or unknown concrete type: decoded value is already correct.
    return decode(data)


def _rehydrate_generic(data: Any, annotation: Any, origin: Any) -> Any:
    """Rehydrate a parametrized annotation by recursing into its arguments.

    Handles ``Annotated[X, ...]`` (unwrapped), unions in both spellings
    (``X | None`` and ``typing.Optional[X]``/``typing.Union[X, Y]``), ``list[X]``,
    ``tuple[X, Y]``/``tuple[X, ...]`` and ``dict[str, X]``, nested to any depth.

    Where the recorded data does not match the annotation's shape, the *decoded* value is
    returned — it is what the first execution produced, so parity across replay is kept.
    Where the annotation demands a reconstruction that cannot be performed, this raises
    :class:`DecodeError`: on the recovery path a loud failure beats a value of the wrong
    type (KAN-474).

    ``data`` is never ``None`` here — :func:`rehydrate` returns that case early.
    """
    args = get_args(annotation)

    if hasattr(annotation, "__metadata__") and args:
        return rehydrate(data, args[0])  # Annotated[X, ...] rehydrates as X

    if _is_union(origin):
        return _rehydrate_union(data, annotation, args)

    if origin in (list, tuple) and isinstance(data, list):
        return _rehydrate_sequence(data, annotation, origin, args)

    if origin is dict and len(args) == 2 and isinstance(data, dict) and data.get(TAG_KEY) is None:
        return _rehydrate_mapping(data, annotation, args)

    if _needs_rehydration(annotation):
        raise DecodeError(
            f"cannot rehydrate a recorded {type(data).__name__!r} against the annotation "
            f"{_annotation_name(annotation)}; the runtime would otherwise hand back a "
            f"plain value of the wrong type on resume only"
        )
    return decode(data)


def _rehydrate_sequence(
    data: list[Any], annotation: Any, origin: Any, args: tuple[Any, ...]
) -> Any:
    """Rehydrate a JSON array against ``list[X]`` or ``tuple[...]``.

    A ``tuple`` annotation reconstructs a real ``tuple`` (the first execution returned
    one; the encoder flattens it to a JSON array), element-wise for the fixed-length
    heterogeneous spelling and uniformly for ``tuple[X, ...]``.
    """
    if origin is tuple:
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(rehydrate(item, args[0]) for item in data)
        if len(args) == len(data):
            return tuple(rehydrate(item, arg) for item, arg in zip(data, args, strict=True))
        if _needs_rehydration(annotation):
            raise DecodeError(
                f"recorded array of {len(data)} element(s) does not match the arity of "
                f"{_annotation_name(annotation)}; cannot rehydrate its elements"
            )
        return tuple(decode(item) for item in data)

    elem = args[0] if args else None
    return [rehydrate(item, elem) for item in data]


def _rehydrate_mapping(data: dict[str, Any], annotation: Any, args: tuple[Any, ...]) -> Any:
    """Rehydrate a JSON object against ``dict[K, V]``, recursing over the *values*."""
    key_type, value_type = args
    if data and not _is_json_key_type(key_type):
        raise DecodeError(
            f"cannot rehydrate {_annotation_name(annotation)}: JSON object keys are always "
            f"strings, so a {_annotation_name(key_type)} key cannot be restored (encode() "
            f"rejects non-string dict keys for the same reason) — declare str keys"
        )
    return {k: rehydrate(v, value_type) for k, v in data.items()}


def _rehydrate_union(data: Any, annotation: Any, args: tuple[Any, ...]) -> Any:
    """Rehydrate against a union, selecting the arm the recorded value belongs to.

    A recorded ``None`` never reaches here (:func:`rehydrate` returns it early), so the
    ``None`` arm of an ``X | None`` is only ever dropped from the candidate set.
    """
    arms = [arm for arm in args if arm is not _NONE_TYPE]
    if not any(_needs_rehydration(arm) for arm in arms):
        return decode(data)  # nothing to reconstruct: the decoded value is already right
    if len(arms) == 1:
        return rehydrate(data, arms[0])  # X | None with a value in hand
    return rehydrate(data, _select_union_arm(data, arms, annotation))


def _select_union_arm(data: Any, arms: list[Any], annotation: Any) -> Any:
    """Pick the union arm for ``data``, preferring the discriminator the encoder wrote.

    Two signals, in order of strength:

    1. the ``"type"`` qualname the encoder records on a tagged value — exact, and since
       KAN-520 it survives :func:`decode` on a :class:`TaggedDict`, so the replay path
       gets it too. It is only ever **compared** to each arm's qualname, never resolved
       or imported, so no module-path coupling is created (ADR-0005/ADR-0031);
    2. the encoded shape (array / object / primitive / natively-decoded object).

    Neither narrowing to exactly one arm is a hard error, never a guess. There used to be
    a third signal — narrowing objects by their declared field names — and it was only
    ever needed because signal 1 was thrown away on read; it is deleted with the cause,
    because "two arms with the same fields" is precisely where guessing is dangerous.
    """
    kind, recorded = _tag_of(data)
    if kind in _TYPED_KINDS and isinstance(recorded, str):
        for arm in arms:
            if isinstance(arm, type) and _qualname(arm) == recorded:
                return arm

    candidates = [arm for arm in arms if _arm_accepts(data, kind, arm)]
    if len(candidates) == 1:
        return candidates[0]
    raise DecodeError(
        f"cannot tell which arm of {_annotation_name(annotation)} the recorded "
        f"{_recorded_kind(data, kind, recorded)} belongs to"
        + (f" ({len(candidates)} arms match)" if candidates else " (no arm matches)")
        + _discriminator_hint(recorded)
        + "; annotate the task with a single concrete type, or with a union whose arms are "
        "distinguishable in the journal"
    )


def _discriminator_hint(recorded: Any) -> str:
    """Name the two ways the encoder's discriminator goes missing, when it has.

    Worth spelling out because neither is visible from the value in hand: write-time
    redaction can mask it (ADR-0029 — a hostile pattern set reaches it, the default one
    does not), and a journal recorded before KAN-520 by a *fork* lost it on the copy.
    """
    if recorded == REDACTED:
        return "; its type discriminator was masked by write-time redaction (ADR-0029)"
    if recorded is None:
        return "; the payload carries no recorded type discriminator"
    return ""


def _tag_of(data: Any) -> tuple[str | None, str | None]:
    """Return ``(kind, recorded_type)`` for a value in either form the runtime sees.

    Still-encoded (``rehydrate`` called on a payload straight off ``encode``) reads the
    ``$satay``/``type`` keys; decoded (the replay path, via ``SQLiteStore``) reads the
    :class:`TaggedDict` attributes. ``(None, None)`` for anything else — a plain JSON
    value, or a structured payload whose tag never survived.
    """
    if isinstance(data, TaggedDict):
        return data.satay_kind, data.satay_type
    if isinstance(data, dict):
        tag = data.get(TAG_KEY)
        if isinstance(tag, str):
            recorded = data.get("type")
            return tag, recorded if isinstance(recorded, str) else None
    return None, None


def _arm_accepts(data: Any, tag: str | None, arm: Any) -> bool:
    """Whether ``arm`` could be the type of the recorded ``data``, by encoded shape."""
    if tag is not None:
        if tag == "datetime":
            return arm is datetime
        if tag == "timedelta":
            return arm is timedelta
        if tag == "enum":
            return isinstance(arm, type) and issubclass(arm, enum.Enum)
        if tag in ("dataclass", "model"):
            return _is_structured(arm)
        return False

    if isinstance(arm, type) and issubclass(arm, enum.Enum):
        # decode() drops an enum to its raw value; only a member value can be this arm.
        return any(data == member.value for member in arm)

    origin = get_origin(arm)
    if isinstance(data, list):
        return origin in (list, tuple) or arm in (list, tuple)
    if isinstance(data, dict):
        return origin is dict or arm is dict or _is_structured(arm)
    if isinstance(data, bool):
        return arm is bool
    if isinstance(data, int):
        return arm is int or arm is float
    if isinstance(data, float):
        return arm is float
    if isinstance(data, str):
        return arm is str
    # datetime/timedelta come back from decode() as native objects.
    return isinstance(arm, type) and isinstance(data, arm)


def _recorded_kind(data: Any, tag: str | None, recorded: str | None) -> str:
    if tag in _TYPED_KINDS:
        return f"{tag} {recorded!r}"
    name = "dict" if isinstance(data, dict) else type(data).__name__
    return f"{name!r} value"


def _needs_rehydration(annotation: Any) -> bool:
    """Whether ``annotation`` describes a type that :func:`decode` alone cannot produce.

    ``True`` for dataclasses, Pydantic-shaped models and enums (and any generic
    parametrized by one). ``False`` for JSON-native types and for datetime/timedelta,
    which :func:`decode` already restores from their tags.
    """
    if annotation is None or annotation is Any or _is_empty(annotation):
        return False
    if get_origin(annotation) is not None:
        return any(_needs_rehydration(arg) for arg in get_args(annotation))
    if not isinstance(annotation, type):
        return False
    if issubclass(annotation, enum.Enum):
        return True
    return _is_structured(annotation)


def _is_structured(annotation: Any) -> bool:
    """Whether ``annotation`` is a dataclass or a Pydantic-shaped (duck-typed) model."""
    if not isinstance(annotation, type):
        return False
    return dataclasses.is_dataclass(annotation) or callable(
        getattr(annotation, "model_validate", None)
    )


def _is_union(origin: Any) -> bool:
    """Whether ``origin`` is a union origin, in either spelling.

    ``X | None`` reports ``types.UnionType`` and ``typing.Optional[X]`` reports
    ``typing.Union`` (they are the same object from Python 3.14 on).
    """
    return origin is types.UnionType or origin is typing.Union


def _is_json_key_type(annotation: Any) -> bool:
    """Whether ``annotation`` is a dict-key type a JSON object can round-trip."""
    if annotation is Any or annotation is object or _is_empty(annotation):
        return True
    return isinstance(annotation, type) and issubclass(annotation, str)


def _annotation_name(annotation: Any) -> str:
    return _qualname(annotation) if isinstance(annotation, type) else str(annotation)


def _raw_fields(data: Any) -> dict[str, Any]:
    """Return the decoded field dict for a structured value (for ``model_validate``)."""
    return {k: decode(v) for k, v in _encoded_fields(data).items()}


def _encoded_fields(data: Any) -> dict[str, Any]:
    """Return the field dict of a structured value, without unwrapping its own tags.

    Fields come back exactly as recorded — still encoded for a tagged payload, already
    decoded (but still :class:`TaggedDict`-carrying, so nested unions stay exact) for one
    the store decoded. Either way :func:`rehydrate` recurses over them.
    """
    if isinstance(data, dict):
        if data.get(TAG_KEY) in ("dataclass", "model"):
            fields: dict[str, Any] = data["fields"]
            return dict(fields)
        return {k: v for k, v in data.items() if k != TAG_KEY}
    raise DecodeError(f"expected a structured object to rehydrate, got {type(data).__name__!r}")


def _qualname(tp: type) -> str:
    return f"{tp.__module__}.{tp.__qualname__}"


def _is_empty(annotation: Any) -> bool:
    import inspect

    return annotation is inspect.Signature.empty or annotation is inspect.Parameter.empty
