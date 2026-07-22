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
"""

from __future__ import annotations

import dataclasses
import enum
import json
from datetime import datetime, timedelta
from typing import Any, get_args, get_origin, get_type_hints

#: The discriminator key marking a tagged (non-JSON-native) value.
TAG_KEY = "$satay"


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

    Enums, dataclasses, and Pydantic models decode to a plain dict of fields here;
    :func:`rehydrate` reconstructs the declared Python type when an annotation is
    supplied. Datetimes and timedeltas decode to their native Python objects because
    they are unambiguous from the tag alone.
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
            return {k: decode(v) for k, v in data["fields"].items()}
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
    """
    if annotation is None or annotation is Any or _is_empty(annotation):
        return decode(data)

    origin = get_origin(annotation)
    if origin is not None:
        # Generic alias (list[X], dict[str, X], X | None, ...). Decode structurally;
        # recurse into element annotations where it is unambiguous.
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
        raw = _raw_fields(data)
        hints = get_type_hints(annotation)
        kwargs = {}
        for f in dataclasses.fields(annotation):
            if f.name in raw:
                kwargs[f.name] = rehydrate(raw[f.name], hints.get(f.name))
        return annotation(**kwargs)

    # Primitive or unknown concrete type: decoded value is already correct.
    return decode(data)


def _rehydrate_generic(data: Any, annotation: Any, origin: Any) -> Any:
    args = get_args(annotation)
    if origin in (list, tuple) and args and isinstance(data, list):
        elem = args[0]
        return [rehydrate(item, elem) for item in data]
    return decode(data)


def _raw_fields(data: Any) -> dict[str, Any]:
    """Return the raw (still-encoded-native) field dict for a structured value."""
    if isinstance(data, dict):
        if data.get(TAG_KEY) in ("dataclass", "model"):
            return {k: decode(v) for k, v in data["fields"].items()}
        return {k: decode(v) for k, v in data.items() if k != TAG_KEY}
    raise DecodeError(f"expected a structured object to rehydrate, got {type(data).__name__!r}")


def _qualname(tp: type) -> str:
    return f"{tp.__module__}.{tp.__qualname__}"


def _is_empty(annotation: Any) -> bool:
    import inspect

    return annotation is inspect.Signature.empty or annotation is inspect.Parameter.empty
