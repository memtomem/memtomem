"""Quality Lab search-observation contract tests."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock
from uuid import UUID

from helpers import StubCtx
from memtomem.server.context import AppContext
from memtomem.server.tools.search import mem_search
from memtomem.search.pipeline import SearchPipeline


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


async def test_snapshot_source_name_is_a_basename_never_a_path(bm25_only_components):
    """Value-level pin for the snapshot writer's projection (#1815).

    ``SnapshotEntryOut`` deliberately does not re-sanitize field contents
    (#1813) — the guarantee that ``source_name`` is a bare basename lives
    at the writer. A writer change that records an absolute or relative
    path must fail here, not surface through ``GET /api/search/runs``.
    """
    components, memory_dir = bm25_only_components
    nested = memory_dir / "projects" / "alpha"
    nested.mkdir(parents=True)
    note = nested / "pinned.md"
    note.write_text("# Pin\n\nBasename invariant probe content.\n", encoding="utf-8")
    await components.index_engine.index_file(note)

    results, stats = await components.search_pipeline.search(
        "basename invariant probe", top_k=5, origin="web"
    )

    assert results
    row = (await components.storage.get_query_history(limit=1))[0]
    assert row["run_id"] == stats.query_run_id
    assert row["result_snapshot"]
    for entry in row["result_snapshot"]:
        source_name = entry["source_name"]
        assert source_name == note.name
        # Both separators: catches a path leak regardless of the OS the
        # writer ran on (POSIX "/" and Windows "\\").
        assert "/" not in source_name
        assert "\\" not in source_name


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


async def test_legacy_backend_keeps_fire_and_forget_and_skips_cache_hit_history(
    bm25_only_components,
):
    components, memory_dir = bm25_only_components
    await _index_quality_note(components, memory_dir)

    class LegacyStorageProxy:
        def __init__(self, delegate):
            self._delegate = delegate
            self.save_query_history = AsyncMock()

        def __getattr__(self, name):
            return getattr(self._delegate, name)

    legacy_storage = LegacyStorageProxy(components.storage)
    pipeline = SearchPipeline(
        storage=legacy_storage,  # type: ignore[arg-type]
        embedder=components.embedder,
        config=components.config.search,
    )

    results, first = await pipeline.search("telemetry", origin="internal")
    await asyncio.sleep(0)
    cached_results, second = await pipeline.search("telemetry", origin="internal")
    await asyncio.sleep(0)

    assert results and cached_results
    assert first.query_run_id is None
    assert second.query_run_id is None
    assert second.cache_hit is True
    legacy_storage.save_query_history.assert_awaited_once()


async def test_explicit_instance_observation_capability_is_used(bm25_only_components):
    components, memory_dir = bm25_only_components
    await _index_quality_note(components, memory_dir)

    class InstanceCapabilityProxy:
        def __init__(self, delegate):
            self._delegate = delegate
            self.save_search_observation = AsyncMock(
                side_effect=lambda *args, **kwargs: kwargs["run_id"]
            )

        def __getattr__(self, name):
            return getattr(self._delegate, name)

    proxy = InstanceCapabilityProxy(components.storage)
    pipeline = SearchPipeline(
        storage=proxy,  # type: ignore[arg-type]
        embedder=components.embedder,
        config=components.config.search,
    )

    results, stats = await pipeline.search("telemetry", origin="internal")

    assert results
    assert stats.query_run_id is not None
    proxy.save_search_observation.assert_awaited_once()
