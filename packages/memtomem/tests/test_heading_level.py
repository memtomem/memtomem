"""Unit tests for _heading_level in memtomem.indexing.engine."""

from __future__ import annotations

import pytest

from memtomem.indexing.engine import _heading_level


@pytest.mark.parametrize(
    ("heading", "expected"),
    [
        ("# A", 1),
        ("## A", 2),
        ("### A", 3),
        ("#### A", 4),
        ("##### A", 5),
        ("###### A", 6),
        ("   ## X", 2),
        ("#X", 0),
        ("##\tX", 0),
        ("##", 0),
        ("#######  x", 0),
        ("", 0),
        ("H1", 0),
    ],
)
def test_heading_level(heading: str, expected: int) -> None:
    """Verify _heading_level returns ATX level 1-6 or 0 for non-headings."""
    assert _heading_level(heading) == expected
