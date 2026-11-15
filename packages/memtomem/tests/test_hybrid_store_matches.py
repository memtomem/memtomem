"""Contract tests for the LangGraph hybrid store's value filters."""

import pytest

from memtomem.storage.hybrid_store import matches


@pytest.mark.parametrize(
    ("actual", "filter_value", "expected"),
    [
        pytest.param({"n": 2}, {"n": 3}, False, id="different-field-value"),
        pytest.param({"a": {"b": 1, "c": 2}}, {"a": {"b": 1}}, True, id="partial-object"),
        pytest.param({"a": {"b": 1, "c": 2}}, {"a": {"b": 2}}, False, id="nested-mismatch"),
        pytest.param({"a": {"b": 1}}, {"a.b": 2}, False, id="dotted-path-mismatch"),
        pytest.param({"a": {"b": 1}}, {"a.x": 1}, False, id="missing-path"),
        pytest.param({"a": 1}, {"a.b": 1}, False, id="path-through-scalar"),
        pytest.param({"x": 1}, {"x": 1, "y": 2}, False, id="all-keys-must-match"),
        pytest.param([1, 2], [1], False, id="different-array-length"),
        pytest.param([1, 2], [2, 1], False, id="different-array-order"),
        pytest.param(4, {"$in": [2, 3]}, False, id="in-rejects-absent-value"),
        pytest.param(2, {"$nin": [2]}, False, id="nin-rejects-present-value"),
        pytest.param(2, {"$eq": 3}, False, id="eq-rejects-different-value"),
        pytest.param(2, {"$ne": 2}, False, id="ne-rejects-equal-value"),
        pytest.param(1, {"$gt": 1}, False, id="gt-rejects-equal-value"),
        pytest.param(0, {"$gte": 1}, False, id="gte-rejects-smaller-value"),
        pytest.param(3, {"$lt": 3}, False, id="lt-rejects-equal-value"),
        pytest.param(4, {"$lte": 3}, False, id="lte-rejects-larger-value"),
        pytest.param(2, {"$gt": 1, "$lt": 2}, False, id="all-operators-must-match"),
        pytest.param(None, {"$gt": 1}, False, id="cannot-order-none-and-int"),
        pytest.param("a", {"$gt": 1}, False, id="cannot-order-str-and-int"),
    ],
)
def test_matches(actual: object, filter_value: object, expected: bool) -> None:
    assert matches(actual, filter_value) is expected


def test_matches_rejects_unknown_operator() -> None:
    with pytest.raises(ValueError, match="Unsupported filter operator"):
        matches(1, {"$typo": 1})
