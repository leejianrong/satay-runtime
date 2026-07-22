"""Unit tests for the JSON codec and typed rehydration (N12, ADR-0005)."""

from __future__ import annotations

import dataclasses
import enum
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from satay.journal.codec import (
    EncodeError,
    decode,
    encode,
    from_json,
    rehydrate,
    to_json,
)


class Color(enum.Enum):
    RED = "red"
    GREEN = "green"


@dataclasses.dataclass
class Point:
    x: int
    y: int


@dataclasses.dataclass
class Segment:
    start: Point
    label: str


def test_primitives_pass_through() -> None:
    for value in [None, True, 3, 3.5, "hi", [1, 2, 3], {"a": 1}]:
        assert from_json(to_json(value)) == value


def test_datetime_round_trips_through_tagged_form() -> None:
    dt = datetime(2026, 7, 22, 10, 30, tzinfo=UTC)
    assert encode(dt) == {"$satay": "datetime", "v": dt.isoformat()}
    assert from_json(to_json(dt)) == dt


def test_timedelta_round_trips() -> None:
    td = timedelta(seconds=90)
    assert from_json(to_json(td)) == td


def test_enum_round_trips_by_value_with_type_tag() -> None:
    encoded = encode(Color.GREEN)
    assert encoded["$satay"] == "enum"
    assert encoded["v"] == "green"
    # decode() drops to the raw value; rehydrate() restores the enum type.
    assert decode(encoded) == "green"
    assert rehydrate(encoded, Color) is Color.GREEN


def test_unencodable_type_names_the_path() -> None:
    with pytest.raises(EncodeError) as excinfo:
        encode({"outer": {"inner": object()}})
    assert "$.outer.inner" in str(excinfo.value)


def test_non_string_dict_key_rejected() -> None:
    with pytest.raises(EncodeError):
        encode({1: "no"})


def test_rehydrate_dataclass_from_stored_dict() -> None:
    seg = Segment(start=Point(1, 2), label="edge")
    restored = rehydrate(from_json(to_json(seg)), Segment)
    assert isinstance(restored, Segment)
    assert isinstance(restored.start, Point)
    assert restored.start.x == 1
    assert restored.label == "edge"


def test_rehydrate_falls_back_to_dict_when_unannotated() -> None:
    seg = Segment(start=Point(1, 2), label="edge")
    restored = rehydrate(from_json(to_json(seg)), None)
    assert isinstance(restored, dict)
    assert restored["label"] == "edge"


def test_rehydrate_pydantic_model_when_available() -> None:
    pydantic = pytest.importorskip("pydantic")

    class User(pydantic.BaseModel):
        name: str
        age: int

    user = User(name="ada", age=36)
    encoded = encode(user)
    assert encoded["$satay"] == "model"
    restored = rehydrate(encoded, User)
    assert isinstance(restored, User)
    assert restored.name == "ada"
    assert restored.age == 36


class DuckModel:
    """A Pydantic-shaped stand-in: the codec is duck-typed, not pydantic-coupled."""

    def __init__(self, name: str, age: int) -> None:
        self.name = name
        self.age = age

    def model_dump(self, *, mode: str = "python") -> dict[str, object]:
        return {"name": self.name, "age": self.age}

    @classmethod
    def model_validate(cls, data: dict[str, object]) -> DuckModel:
        return cls(name=str(data["name"]), age=int(data["age"]))  # type: ignore[call-overload]


def test_duck_typed_model_encodes_and_rehydrates_without_pydantic() -> None:
    obj = DuckModel("grace", 45)
    encoded = encode(obj)
    assert encoded["$satay"] == "model"
    restored = rehydrate(encoded, DuckModel)
    assert isinstance(restored, DuckModel)
    assert restored.name == "grace"
    assert restored.age == 45


def test_no_pickle_import_in_codec() -> None:
    import satay.journal.codec as codec_module

    src = codec_module.__file__
    assert src is not None
    text = Path(src).read_text()
    # Only the "no pickle" doc phrasing may appear; never an import/use of pickle.
    assert "import pickle" not in text
    assert "pickle.dumps" not in text
    assert "pickle.loads" not in text
