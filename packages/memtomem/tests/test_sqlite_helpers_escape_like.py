"""Direct tests for escape_like (escaping and order).

See https://github.com/memtomem/memtomem/issues/2985
"""

from memtomem.storage.sqlite_helpers import escape_like


def test_escape_like_underscore() -> None:
    assert escape_like(r"a_b") == r"a\_b"


def test_escape_like_percent() -> None:
    assert escape_like(r"100%") == r"100\%"


def test_escape_like_backslash() -> None:
    assert escape_like(r"C:\x") == r"C:\\x"


def test_escape_like_backslash_then_percent() -> None:
    # Backslash must be escaped first, otherwise the % escape would be re-escaped.
    assert escape_like(r"\%") == r"\\\%"


def test_escape_like_plain() -> None:
    assert escape_like(r"plain") == r"plain"
