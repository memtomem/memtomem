"""``_is_valid_ns_chars`` — the storage-level namespace character check."""

from __future__ import annotations

import pytest

from memtomem.storage.sqlite_namespace import _is_valid_ns_chars


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("default", True),
        ("agent-runtime:planner", True),
        ("claude-memory:my proj@v1.2", True),
        ("a" * 255, True),
        ("a" * 256, False),
        ("", False),
        ("foo\n", False),  # ``match`` + ``$`` used to accept a final newline
        ("foo\nbar", False),
        ("foo\x00", False),
    ],
)
def test_is_valid_ns_chars(name: str, expected: bool) -> None:
    assert _is_valid_ns_chars(name) is expected
