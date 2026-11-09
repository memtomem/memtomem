"""Direct tests for parse_tag_filter (#3138)."""

from __future__ import annotations

import pytest
from memtomem.storage.base import parse_tag_filter


@pytest.mark.parametrize(
    ("tag_filter", "expected"),
    [
        (None, ()),
        ("", ()),
        (",", ()),
        (" , ", ()),
        ("  solo  ", ("solo",)),
        ("z,y,x", ("x", "y", "z")),
        ("b, a ,b", ("a", "b")),
        ("x,,y", ("x", "y")),
        ("A,a", ("A", "a")),
        ("a b,c", ("a b", "c")),
    ],
)
def test_parse_tag_filter_cases(tag_filter: str | None, expected: tuple[str, ...]) -> None:
    """Cover the documented edge cases and contract of parse_tag_filter."""
    assert parse_tag_filter(tag_filter) == expected
