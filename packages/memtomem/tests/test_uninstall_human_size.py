"""Direct tests for ``_human_size``, the size column of ``mm uninstall`` (#3412)."""

from __future__ import annotations

import pytest

from memtomem.cli.uninstall_cmd import _human_size


@pytest.mark.parametrize(
    ("n", "expected"),
    [
        (0, "0 B"),
        (1, "1 B"),
        (1023, "1023 B"),
        (1024, "1.0 KB"),
        (1536, "1.5 KB"),
        (1048576, "1.0 MB"),
        (1073741824, "1.0 GB"),
        # GB is the largest unit, so 5 TiB stays in GB.
        (5497558138880, "5120.0 GB"),
    ],
)
def test_human_size(n: int, expected: str) -> None:
    assert _human_size(n) == expected
