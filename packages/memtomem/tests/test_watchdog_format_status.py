"""Direct tests for ``_format_status``, the status text of ``mem_watchdog`` (#3257)."""

from __future__ import annotations

import pytest

from memtomem.server.tools.watchdog import _format_status


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        pytest.param({}, ["Health watchdog is not enabled."], id="empty"),
        pytest.param(
            {"enabled": False, "running": True},
            ["Health watchdog is not enabled."],
            id="disabled-even-if-running",
        ),
        pytest.param(
            {"enabled": True, "running": True},
            ["Health watchdog: running", "  No checks recorded yet."],
            id="running-no-checks-key",
        ),
        pytest.param(
            {"enabled": True, "running": False, "checks": {}},
            ["Health watchdog: stopped", "  No checks recorded yet."],
            id="stopped-empty-checks",
        ),
        pytest.param(
            {
                "enabled": True,
                "checks": {"x": {"status": "critical", "tier": "deep", "value": None}},
            },
            ["Health watchdog: stopped", "  ✗ x [deep]: critical — null"],
            id="critical-missing-running",
        ),
        pytest.param(
            {
                "enabled": True,
                "running": True,
                "checks": {
                    "wal_status": {
                        "status": "ok",
                        "tier": "heartbeat",
                        "value": {"wal_pages": 0},
                    },
                    "orphan_count": {"status": "warning", "tier": "diagnostic", "value": 3},
                },
            },
            [
                "Health watchdog: running",
                "  ⚠ orphan_count [diagnostic]: warning — 3",
                '  ✓ wal_status [heartbeat]: ok — {"wal_pages": 0}',
            ],
            id="warning-and-ok-sorted",
        ),
    ],
)
def test_format_status(data: dict, expected: list[str]) -> None:
    assert [ln for ln in _format_status(data).splitlines() if ln] == expected
