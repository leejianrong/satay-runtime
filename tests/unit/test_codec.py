"""Unit tests for the JSON codec and typed rehydration (N12, ADR-0005)."""

from __future__ import annotations

import dataclasses
import enum
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Optional, Union

import pytest

from satay.journal.codec import (
    DecodeError,
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


class Size(enum.Enum):
    SMALL = "small"
    LARGE = "large"


@dataclasses.dataclass
class Point:
    x: int
    y: int


@dataclasses.dataclass
class Segment:
    start: Point
    label: str


@dataclasses.dataclass
class Label:
    text: str


@dataclasses.dataclass
class Boxed:
    head: Point | None
    tags: list[Label]
    index: dict[str, Point]


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


# ---------------------------------------------------------------------------
# The annotation matrix (KAN-474): a resumed value must have the same Python type
# as the first-execution value, for every shape an author can annotate.
#
# Each case is checked twice, because both forms reach rehydrate() in the runtime:
#   * the still-encoded payload (tags present), and
#   * the decoded payload (tags already consumed) — what SQLiteStore hands the
#     replay engine on resume.
# ---------------------------------------------------------------------------

_DT = datetime(2026, 7, 22, 10, 30, tzinfo=UTC)

MATRIX: list[tuple[str, Any, Any]] = [
    ("optional_pep604", Point | None, Point(1, 2)),
    ("optional_pep604_none", Point | None, None),
    ("optional_typing", Optional[Point], Point(1, 2)),  # noqa: UP045 — the spelling is the point
    ("union_typing_two_arms", Union[Point, Label], Label(text="x")),  # noqa: UP007 — ditto
    ("union_pep604_two_arms", Point | Label, Point(3, 4)),
    ("union_of_primitives_int", int | str, 5),
    ("union_of_primitives_str", int | str, "five"),
    ("union_of_enums", Color | Size, Size.LARGE),
    ("optional_enum", Color | None, Color.RED),
    ("optional_datetime", datetime | None, _DT),
    ("list_of_dataclass", list[Point], [Point(1, 2), Point(3, 4)]),
    ("list_empty", list[Point], []),
    ("list_of_enum", list[Color], [Color.RED, Color.GREEN]),
    ("dict_of_dataclass", dict[str, Point], {"a": Point(1, 2)}),
    ("dict_empty", dict[str, Point], {}),
    ("dict_of_optional", dict[str, Point | None], {"a": Point(1, 2), "b": None}),
    ("dict_of_any", dict[str, Any], {"a": 1, "b": "two"}),
    ("nested_list_of_dict", list[dict[str, Point]], [{"a": Point(1, 2)}]),
    ("nested_dict_of_list", dict[str, list[Point]], {"a": [Point(1, 2)]}),
    ("nested_list_of_list", list[list[Point]], [[Point(1, 2)]]),
    ("optional_list", list[Point] | None, [Point(1, 2)]),
    ("optional_list_none", list[Point] | None, None),
    ("optional_dict", dict[str, Point] | None, {"a": Point(1, 2)}),
    ("tuple_heterogeneous", tuple[Point, int], (Point(1, 2), 7)),
    ("tuple_variadic", tuple[Point, ...], (Point(1, 2), Point(3, 4))),
    ("tuple_empty", tuple[Point, ...], ()),
    ("dataclass_with_generic_fields", Boxed, Boxed(Point(1, 2), [Label("t")], {"k": Point(0, 0)})),
    ("dataclass_with_none_field", Boxed, Boxed(None, [], {})),
    ("annotated_dataclass", Annotated[Point, "meta"], Point(1, 2)),
]


@pytest.mark.parametrize(
    ("annotation", "value"),
    [case[1:] for case in MATRIX],
    ids=[case[0] for case in MATRIX],
)
def test_rehydrate_preserves_the_declared_type(annotation: Any, value: Any) -> None:
    tagged = rehydrate(encode(value), annotation)
    assert tagged == value
    assert type(tagged) is type(value)

    replayed = rehydrate(from_json(to_json(value)), annotation)  # the resume path
    assert replayed == value
    assert type(replayed) is type(value)


def test_union_arm_is_picked_by_the_recorded_type_tag_when_present() -> None:
    """Ambiguous-by-shape arms still resolve while the encoder's discriminator survives."""

    @dataclasses.dataclass
    class Alpha:
        value: int

    @dataclasses.dataclass
    class Beta:
        value: int

    assert isinstance(rehydrate(encode(Alpha(1)), Alpha | Beta), Alpha)
    assert isinstance(rehydrate(encode(Beta(1)), Alpha | Beta), Beta)


def test_indistinguishable_union_arms_fail_loudly() -> None:
    """No tag and no distinguishing field: raise, never guess (the KAN-474 constraint)."""

    @dataclasses.dataclass
    class Alpha:
        value: int

    @dataclasses.dataclass
    class Beta:
        value: int

    with pytest.raises(DecodeError) as excinfo:
        rehydrate(from_json(to_json(Alpha(1))), Alpha | Beta)
    assert "Alpha" in str(excinfo.value) and "Beta" in str(excinfo.value)


def test_non_str_dict_keys_fail_loudly_and_name_the_annotation() -> None:
    with pytest.raises(DecodeError) as excinfo:
        rehydrate({"1": encode(Point(1, 2))}, dict[int, Point])
    assert "int" in str(excinfo.value)
    # An empty mapping has no keys to lose, so it still rehydrates.
    assert rehydrate({}, dict[int, Point]) == {}


def test_unsupported_generic_shape_fails_loudly_rather_than_degrading() -> None:
    with pytest.raises(DecodeError) as excinfo:
        rehydrate([encode(Point(1, 2))], set[Point])
    assert "set" in str(excinfo.value)
    # ...but a shape needing no reconstruction stays permissive.
    assert rehydrate([1, 2], set[int]) == [1, 2]


def test_fixed_tuple_arity_mismatch_fails_loudly() -> None:
    with pytest.raises(DecodeError):
        rehydrate([encode(Point(1, 2)), 7, 8], tuple[Point, int])


def test_recorded_none_is_returned_for_a_container_annotation() -> None:
    """The recorded value wins over an over-promising annotation: no resume-only crash."""
    assert rehydrate(None, list[Point]) is None
    assert rehydrate(None, dict[str, Point]) is None


def test_no_pickle_import_in_codec() -> None:
    import satay.journal.codec as codec_module

    src = codec_module.__file__
    assert src is not None
    text = Path(src).read_text()
    # Only the "no pickle" doc phrasing may appear; never an import/use of pickle.
    assert "import pickle" not in text
    assert "pickle.dumps" not in text
    assert "pickle.loads" not in text
