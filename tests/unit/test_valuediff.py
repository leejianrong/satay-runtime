"""The structural value diff (ADR-0034). Pure, no store, no runs.

Every case here is a shape the journal can actually hold after ``decode``.
"""

from __future__ import annotations

import pytest

from satay.redaction import REDACTED
from satay.valuediff import MAX_DEPTH, MAX_PATHS, ROOT, diff_values


@pytest.mark.parametrize(
    ("a", "b"),
    [
        pytest.param(1, 1, id="scalars"),
        pytest.param("x", "x", id="strings"),
        pytest.param(None, None, id="nones"),
        pytest.param({"a": 1}, {"a": 1}, id="mappings"),
        pytest.param([1, [2, {"k": "v"}]], [1, [2, {"k": "v"}]], id="nested"),
        pytest.param({}, {}, id="empty-mappings"),
        pytest.param({"a": 1, "b": 2}, {"b": 2, "a": 1}, id="mapping-key-order"),
    ],
)
def test_identical_values_report_no_change(a: object, b: object) -> None:
    """Key order is not a difference: the same mapping written two ways is one value."""
    result = diff_values(a, b)
    assert result["changed"] is False
    assert result["paths"] == []


@pytest.mark.parametrize(
    ("a", "b", "paths"),
    [
        pytest.param(1, 2, [ROOT], id="scalar-change-is-not-localisable"),
        pytest.param({"a": 1}, {"a": 2}, [".a"], id="mapping-leaf"),
        pytest.param({"a": {"b": 1}}, {"a": {"b": 2}}, [".a.b"], id="nested-mapping-leaf"),
        pytest.param(["x", "y"], ["x", "z"], ["[1]"], id="argument-index"),
        pytest.param([{"t": 1}], [{"t": 2}], ["[0].t"], id="index-then-key"),
        pytest.param({"a": 1, "b": 2}, {"a": 1}, [".b"], id="key-only-on-a"),
        pytest.param({"a": 1}, {"a": 1, "c": 3}, [".c"], id="key-only-on-b"),
        pytest.param([1, 2, 3], [1, 2], [ROOT], id="length-change-is-the-node"),
        pytest.param({"a": 1}, [1], [ROOT], id="type-mismatch"),
        pytest.param("abc", "abd", [ROOT], id="strings-are-not-walked-as-sequences"),
    ],
)
def test_a_difference_is_reported_at_its_path(a: object, b: object, paths: list[str]) -> None:
    result = diff_values(a, b)
    assert result["changed"] is True
    assert result["paths"] == paths
    assert result["truncated"] is False


def test_only_the_differing_leaf_is_reported() -> None:
    """The point of the feature: which field changed, not that something did."""
    a = {"topic": "acme", "style": "dry", "opts": {"depth": 2, "cite": True}}
    b = {"topic": "acme", "style": "sceptical", "opts": {"depth": 2, "cite": True}}
    assert diff_values(a, b)["paths"] == [".style"]


def test_a_length_change_reports_the_node_not_every_later_element() -> None:
    """Index-by-index pairing after an insertion is noise, not information.

    ``[1, 2, 3]`` vs ``[0, 1, 2, 3]`` differs at every index if paired positionally, which
    would report four changes for one insertion.
    """
    assert diff_values([1, 2, 3], [0, 1, 2, 3])["paths"] == [ROOT]


# --- redaction ------------------------------------------------------------------------


def test_two_masked_leaves_are_unknown_not_identical() -> None:
    """The false negative this design exists to avoid.

    Two *different* secrets both masked in the journal are not equal — their equality is
    unknown. Reporting ``changed=False`` alone would be a confident wrong answer, so the
    ``redacted`` flag says the comparison could not be made.
    """
    result = diff_values({"token": REDACTED}, {"token": REDACTED})
    assert result["changed"] is False
    assert result["redacted"] is True


def test_a_masked_leaf_against_a_visible_one_is_a_difference() -> None:
    """One side masked and the other not: the values provably differ."""
    result = diff_values({"token": REDACTED}, {"token": "visible"})
    assert result["changed"] is True
    assert result["paths"] == [".token"]


def test_an_unmasked_diff_does_not_set_the_redacted_flag() -> None:
    assert diff_values({"a": 1}, {"a": 2})["redacted"] is False


# --- caps -----------------------------------------------------------------------------


def test_paths_are_capped_and_the_cap_is_reported() -> None:
    """Recorded values are unbounded, and Studio re-polls compare every couple of seconds."""
    a = {f"k{i}": i for i in range(MAX_PATHS * 4)}
    b = {f"k{i}": i + 1 for i in range(MAX_PATHS * 4)}
    result = diff_values(a, b)
    assert len(result["paths"]) == MAX_PATHS
    assert result["truncated"] is True
    assert result["changed"] is True


def test_depth_is_capped_and_reported_rather_than_descended() -> None:
    def nest(depth: int, leaf: object) -> object:
        value: object = leaf
        for _ in range(depth):
            value = {"n": value}
        return value

    result = diff_values(nest(MAX_DEPTH * 2, 1), nest(MAX_DEPTH * 2, 2))
    assert result["changed"] is True
    assert result["truncated"] is True
    assert len(result["paths"]) == 1
    assert result["paths"][0].count(".n") == MAX_DEPTH


def test_a_deep_but_shallow_difference_is_not_truncated() -> None:
    """The depth cap must not fire on a difference that sits above it."""
    a = {"n": {"n": {"leaf": 1}}}
    b = {"n": {"n": {"leaf": 2}}}
    result = diff_values(a, b)
    assert result["paths"] == [".n.n.leaf"]
    assert result["truncated"] is False
