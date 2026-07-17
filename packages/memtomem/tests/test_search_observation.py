"""Quality Lab search-observation contract tests."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock
from uuid import UUID

from helpers import StubCtx
from memtomem.server.context import AppContext
from memtomem.server.tools.search import mem_search


async def _index_quality_note(components, memory_dir):
    note = memory_dir / "quality.md"
    note.write_text(
        "# Retrieval Quality\n\nDurable observation telemetry for local search.\n",
        encoding="utf-8",
    )
    await components.index_engine.index_file(note)
    return note


async def test_ranked_search_persists_durable_secret_free_observation(
    bm25_only_components,
):
    components, memory_dir = bm25_only_components
    note = await _index_quality_note(components, memory_dir)

    results, stats = await components.search_pipeline.search(
        "관측 telemetry", top_k=5, origin="web"
    )

    assert results
    assert stats.query_run_id is not None
    UUID(stats.query_run_id)
    assert stats.cache_hit is False
    assert stats.latency_ms is not None and stats.latency_ms >= 0

    history = await components.storage.get_query_history(limit=10)
    assert len(history) == 1
    row = history[0]
    assert row["run_id"] == stats.query_run_id
    assert row["query_text"] == "관측 telemetry"
    assert row["observation"]["origin"] == "web"
    assert row["observation"]["query_language"] == "ko"
    assert len(row["observation"]["profile_id"]) == 64
    assert row["observation"]["final_total"] == len(results)
    assert row["result_snapshot"][0]["source_name"] == note.name
    snapshot_json = json.dumps(row["result_snapshot"], ensure_ascii=False)
    assert str(memory_dir) not in snapshot_json
    assert "content" not in row["result_snapshot"][0]


async def test_zero_result_ranked_search_still_gets_run_id(bm25_only_components):
    components, memory_dir = bm25_only_components
    await _index_quality_note(components, memory_dir)

    results, stats = await components.search_pipeline.search(
        "term-that-does-not-exist-xyz", origin="mcp"
    )

    assert results == []
    assert stats.query_run_id is not None
    row = (await components.storage.get_query_history(limit=1))[0]
    assert row["run_id"] == stats.query_run_id
    assert row["observation"]["origin"] == "mcp"
    assert row["observation"]["final_total"] == 0
    assert row["result_snapshot"] == []


async def test_cache_hit_records_distinct_durable_run(bm25_only_components):
    components, memory_dir = bm25_only_components
    await _index_quality_note(components, memory_dir)

    _, first = await components.search_pipeline.search("telemetry", origin="cli")
    _, second = await components.search_pipeline.search("telemetry", origin="cli")

    assert first.query_run_id is not None
    assert second.query_run_id is not None
    assert first.query_run_id != second.query_run_id
    assert first.cache_hit is False
    assert second.cache_hit is True
    rows = await components.storage.get_query_history(limit=10)
    assert {row["run_id"] for row in rows} == {first.query_run_id, second.query_run_id}
    assert {row["observation"]["cache_hit"] for row in rows} == {False, True}


async def test_observation_failure_never_fails_search(bm25_only_components, monkeypatch):
    components, memory_dir = bm25_only_components
    await _index_quality_note(components, memory_dir)
    failing_save = AsyncMock(side_effect=RuntimeError("observation DB unavailable"))
    monkeypatch.setattr(components.storage, "save_search_observation", failing_save)

    results, stats = await components.search_pipeline.search("telemetry", origin="web")

    assert results
    assert stats.query_run_id is None
    failing_save.assert_awaited_once()


async def test_filter_only_search_is_not_observed(bm25_only_components):
    components, memory_dir = bm25_only_components
    note = await _index_quality_note(components, memory_dir)

    results, stats = await components.search_pipeline.search(
        "", source_filter=note.name, origin="web"
    )

    assert results
    assert stats.query_run_id is None
    assert await components.storage.get_query_history(limit=10) == []


async def test_mcp_structured_search_exposes_committed_run_id(bm25_only_components):
    components, memory_dir = bm25_only_components
    await _index_quality_note(components, memory_dir)
    ctx = StubCtx(AppContext.from_components(components))

    output = await mem_search(  # type: ignore[arg-type]
        query="telemetry", output_format="structured", ctx=ctx
    )

    payload = json.loads(output)
    UUID(payload["query_run_id"])
    row = (await components.storage.get_query_history(limit=1))[0]
    assert row["run_id"] == payload["query_run_id"]
    assert row["observation"]["origin"] == "mcp"
