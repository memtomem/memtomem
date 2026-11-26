"""Direct tests for ``_summary_line``, the run summary line of ``mem_policy_list`` (#3223)."""

from __future__ import annotations

import pytest

from memtomem.server.tools.policy import _summary_line


@pytest.mark.parametrize(
    ("summary", "expected"),
    [
        pytest.param({}, None, id="empty"),
        pytest.param({"unknown": 1}, None, id="unknown-kind"),
        pytest.param(
            {"deleted_ids": ["a", "b", "c"]},
            "deleted_ids: 3 — a, b, c",
            id="under-preview-limit",
        ),
        pytest.param(
            {"deleted_ids": ["i0", "i1", "i2", "i3", "i4"]},
            "deleted_ids: 5 — i0, i1, i2, i3, i4",
            id="at-preview-limit",
        ),
        pytest.param(
            {"deleted_ids": ["i0", "i1", "i2", "i3", "i4", "i5"]},
            "deleted_ids: 6 — i0, i1, i2, i3, i4, … (+1 more)",
            id="over-preview-limit",
        ),
        pytest.param(
            {"deleted_ids": ["i0", "i1", "i2", "i3", "i4"], "deleted_ids_omitted": 10},
            "deleted_ids: 15 — i0, i1, i2, i3, i4, … (+10 more)",
            id="omitted-by-run-log",
        ),
        pytest.param(
            {"deleted_ids": ["i0", "i1", "i2", "i3", "i4", "i5"], "deleted_ids_omitted": 10},
            "deleted_ids: 16 — i0, i1, i2, i3, i4, … (+11 more)",
            id="over-limit-and-omitted",
        ),
        pytest.param({"promoted_ids": ["p1"]}, "promoted_ids: 1 — p1", id="promoted"),
        pytest.param(
            {"deleted_ids": ["d"], "promoted_ids": ["p"]},
            "deleted_ids: 1 — d",
            id="deleted-wins",
        ),
        pytest.param(
            {"promoted_ids": ["p"], "deleted_ids": ["d"]},
            "deleted_ids: 1 — d",
            id="deleted-wins-when-inserted-last",
        ),
        pytest.param(
            {"groups": [1, 2], "deleted_summary_ids": ["x"]},
            "2 groups consolidated, 1 stale summaries deleted",
            id="consolidation",
        ),
        pytest.param(
            {"tagged_chunks": 3, "total_chunks": 10, "skipped_chunks": 2},
            "tagged 3/10 (skipped 2)",
            id="tagging",
        ),
        pytest.param({"tagged_chunks": 3}, "tagged 3/? (skipped 0)", id="tagging-defaults"),
    ],
)
def test_summary_line(summary: dict, expected: str | None) -> None:
    assert _summary_line(summary) == expected
