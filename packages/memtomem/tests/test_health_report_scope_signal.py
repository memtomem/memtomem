"""The health report signals "not project-scoped" instead of counting 0 (#2281).

``sessions`` and ``working_memory`` rows carry no ``project_root``, so a
project-scoped report has no per-project count to give. It says so explicitly
(``available: false`` + ``None`` counts) rather than emitting a ``0`` that a
reader cannot distinguish from "this install really has none".
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from memtomem.storage.mixins.analytics import NO_PROJECT_IDENTITY, _unavailable_block


def test_unavailable_block_shape() -> None:
    block = _unavailable_block("total", "active")
    assert block == {
        "total": None,
        "active": None,
        "available": False,
        "reason": NO_PROJECT_IDENTITY,
    }


def _report(**overrides) -> dict:
    report = {
        "total_chunks": 1,
        "total_sources": 1,
        "access_coverage": {"accessed": 1, "total": 1, "pct": 100.0},
        "tag_coverage": {"tagged": 1, "total": 1, "pct": 100.0},
        "dead_memories_pct": 0.0,
        "top_accessed": [],
        "namespace_distribution": [],
        "sessions": _unavailable_block("total", "active", "recent_7d"),
        "working_memory": _unavailable_block("total", "promoted"),
        "cross_references": 0,
    }
    report.update(overrides)
    return report


async def _run_mem_eval(monkeypatch: pytest.MonkeyPatch, report: dict) -> str:
    from memtomem.server.tools.evaluation import mem_eval

    app = MagicMock()
    app.storage.get_health_report = AsyncMock(return_value=report)
    monkeypatch.setattr(
        "memtomem.server.tools.evaluation._get_app_initialized", AsyncMock(return_value=app)
    )
    monkeypatch.setattr("memtomem.server.tools.evaluation.caller_boundary", lambda _app: None)
    return await mem_eval(ctx=SimpleNamespace())


@pytest.mark.asyncio
async def test_mem_eval_notes_the_unavailable_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    # Regression: the old ``sess.get("total", 0) > 0`` guard raised TypeError
    # the moment the count became ``None``.
    out = await _run_mem_eval(monkeypatch, _report())

    assert "### Session Activity" not in out
    assert "### Working Memory" not in out
    assert "no project identity" in out
    assert "Session activity and Working memory not reported" in out


@pytest.mark.asyncio
async def test_mem_eval_still_renders_available_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    out = await _run_mem_eval(
        monkeypatch,
        _report(
            sessions={"total": 4, "active": 1, "recent_7d": 2},
            working_memory={"total": 3, "promoted": 1},
        ),
    )

    assert "- Total sessions: 4" in out
    assert "- Promoted to long-term: 1" in out
    assert "no project identity" not in out
