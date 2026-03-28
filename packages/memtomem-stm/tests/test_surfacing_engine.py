"""Tests for SurfacingEngine — the core proactive memory surfacing orchestrator."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from memtomem_stm.surfacing.config import SurfacingConfig
from memtomem_stm.surfacing.engine import SurfacingEngine


# ── Helpers ──────────────────────────────────────────────────────────────


@dataclass
class FakeChunkMeta:
    source_file: Path = Path("/notes/test.md")
    namespace: str = "default"


@dataclass
class FakeChunk:
    id: str = ""
    content: str = "some memory content"
    metadata: FakeChunkMeta | None = None

    def __post_init__(self):
        if not self.id:
            self.id = str(uuid4())
        if self.metadata is None:
            self.metadata = FakeChunkMeta()


@dataclass
class FakeSearchResult:
    chunk: FakeChunk
    score: float
    rank: int = 1


def _make_config(**overrides) -> SurfacingConfig:
    defaults = {
        "enabled": True,
        "min_response_chars": 10,
        "timeout_seconds": 5.0,
        "min_score": 0.02,
        "max_results": 3,
        "cooldown_seconds": 0.0,
        "max_surfacings_per_minute": 1000,
        "auto_tune_enabled": False,
        "include_session_context": False,
        "fire_webhook": False,
        "cache_ttl_seconds": 60.0,
    }
    defaults.update(overrides)
    return SurfacingConfig(**defaults)


def _make_search_pipeline(results: list[FakeSearchResult] | None = None):
    pipeline = AsyncMock()
    pipeline.search = AsyncMock(return_value=(results or [], {}))
    return pipeline


LONG_RESPONSE = "x" * 200  # above min_response_chars=10

# Arguments that produce a valid query for ContextExtractor
VALID_ARGS = {"path": "src/app.py", "_context_query": "Flask web framework architecture"}


# ── Tests ────────────────────────────────────────────────────────────────


class TestSurfacingBasic:
    async def test_normal_surfacing_injects_memories(self):
        results = [FakeSearchResult(chunk=FakeChunk(content="Flask chosen"), score=0.5)]
        engine = SurfacingEngine(
            config=_make_config(),
            search_pipeline=_make_search_pipeline(results),
        )
        output = await engine.surface("gh", "read_file", VALID_ARGS, LONG_RESPONSE)
        assert "Relevant Memories" in output
        assert "Flask chosen" in output

    async def test_empty_results_returns_original(self):
        engine = SurfacingEngine(
            config=_make_config(),
            search_pipeline=_make_search_pipeline([]),
        )
        output = await engine.surface("gh", "read_file", VALID_ARGS, LONG_RESPONSE)
        assert output == LONG_RESPONSE

    async def test_disabled_returns_original(self):
        engine = SurfacingEngine(
            config=_make_config(enabled=False),
            search_pipeline=_make_search_pipeline([FakeSearchResult(FakeChunk(), 0.9)]),
        )
        output = await engine.surface("gh", "tool", {}, LONG_RESPONSE)
        assert output == LONG_RESPONSE

    async def test_no_search_pipeline_returns_original(self):
        engine = SurfacingEngine(config=_make_config())
        output = await engine.surface("gh", "tool", {}, LONG_RESPONSE)
        assert output == LONG_RESPONSE


class TestSurfacingGating:
    async def test_short_response_skipped(self):
        engine = SurfacingEngine(
            config=_make_config(min_response_chars=1000),
            search_pipeline=_make_search_pipeline([FakeSearchResult(FakeChunk(), 0.9)]),
        )
        output = await engine.surface("gh", "tool", {}, "short")
        assert output == "short"

    async def test_write_tool_skipped(self):
        engine = SurfacingEngine(
            config=_make_config(),
            search_pipeline=_make_search_pipeline([FakeSearchResult(FakeChunk(), 0.9)]),
        )
        output = await engine.surface("fs", "write_file", {"path": "x", "_context_query": "test"}, LONG_RESPONSE)
        assert output == LONG_RESPONSE

    async def test_delete_tool_skipped(self):
        engine = SurfacingEngine(
            config=_make_config(),
            search_pipeline=_make_search_pipeline([FakeSearchResult(FakeChunk(), 0.9)]),
        )
        output = await engine.surface("fs", "delete_file", {"path": "x", "_context_query": "test"}, LONG_RESPONSE)
        assert output == LONG_RESPONSE


class TestSurfacingScoreFilter:
    async def test_below_min_score_filtered(self):
        results = [FakeSearchResult(chunk=FakeChunk(), score=0.01)]
        engine = SurfacingEngine(
            config=_make_config(min_score=0.02),
            search_pipeline=_make_search_pipeline(results),
        )
        output = await engine.surface("gh", "read_file", VALID_ARGS, LONG_RESPONSE)
        assert output == LONG_RESPONSE  # filtered, no injection

    async def test_at_min_score_included(self):
        results = [FakeSearchResult(chunk=FakeChunk(content="exactly at threshold"), score=0.02)]
        engine = SurfacingEngine(
            config=_make_config(min_score=0.02),
            search_pipeline=_make_search_pipeline(results),
        )
        output = await engine.surface("gh", "read_file", VALID_ARGS, LONG_RESPONSE)
        assert "exactly at threshold" in output

    async def test_max_results_limit(self):
        results = [
            FakeSearchResult(chunk=FakeChunk(content=f"result-{i}"), score=0.5 - i * 0.01)
            for i in range(10)
        ]
        engine = SurfacingEngine(
            config=_make_config(max_results=2),
            search_pipeline=_make_search_pipeline(results),
        )
        output = await engine.surface("gh", "read_file", VALID_ARGS, LONG_RESPONSE)
        assert "result-0" in output
        assert "result-1" in output
        assert "result-5" not in output


class TestSurfacingCircuitBreaker:
    async def test_circuit_breaker_opens_after_failures(self):
        failing_pipeline = AsyncMock()
        failing_pipeline.search = AsyncMock(side_effect=RuntimeError("boom"))

        engine = SurfacingEngine(
            config=_make_config(circuit_max_failures=2, circuit_reset_seconds=60),
            search_pipeline=failing_pipeline,
        )

        # First 2 failures should still return original (caught by except)
        await engine.surface("gh", "read_file", VALID_ARGS, LONG_RESPONSE)
        await engine.surface("gh", "read_file", {"path": "y"}, LONG_RESPONSE)

        # Circuit should now be open — pipeline.search NOT called
        failing_pipeline.search.reset_mock()
        output = await engine.surface("gh", "read_file", {"path": "z"}, LONG_RESPONSE)
        assert output == LONG_RESPONSE
        failing_pipeline.search.assert_not_called()


class TestSurfacingTimeout:
    async def test_timeout_returns_original(self):
        async def slow_search(*args, **kwargs):
            await asyncio.sleep(10)
            return [], {}

        pipeline = AsyncMock()
        pipeline.search = slow_search

        engine = SurfacingEngine(
            config=_make_config(timeout_seconds=0.1),
            search_pipeline=pipeline,
        )
        output = await engine.surface("gh", "read_file", VALID_ARGS, LONG_RESPONSE)
        assert output == LONG_RESPONSE


class TestSessionDedup:
    """Verify same memory isn't surfaced twice in one session."""

    async def test_same_memory_not_repeated(self):
        """Second surfacing call should skip already-seen memories."""
        chunk1 = FakeChunk(content="memory A")
        chunk2 = FakeChunk(content="memory B")
        results = [
            FakeSearchResult(chunk=chunk1, score=0.5),
            FakeSearchResult(chunk=chunk2, score=0.4),
        ]
        engine = SurfacingEngine(
            config=_make_config(cooldown_seconds=0),
            search_pipeline=_make_search_pipeline(results),
        )

        out1 = await engine.surface("s", "read_file", VALID_ARGS, LONG_RESPONSE)
        assert "memory A" in out1
        assert "memory B" in out1

        # Clear cache to force re-search, but dedup should filter
        engine._cache.clear()
        out2 = await engine.surface(
            "s", "read_file",
            {"path": "/other", "_context_query": "different query for search"},
            LONG_RESPONSE,
        )
        # Both memories already surfaced → should not appear again
        assert "memory A" not in out2
        assert "memory B" not in out2


class TestSurfacingCache:
    async def test_cache_hit_skips_search(self):
        results = [FakeSearchResult(chunk=FakeChunk(content="cached memory"), score=0.5)]
        pipeline = _make_search_pipeline(results)

        engine = SurfacingEngine(
            config=_make_config(cooldown_seconds=0),
            search_pipeline=pipeline,
        )

        # First call — searches
        out1 = await engine.surface("gh", "read_file", VALID_ARGS, LONG_RESPONSE)
        assert "cached memory" in out1
        assert pipeline.search.call_count == 1

        # Second call — cache hit, no search
        out2 = await engine.surface("gh", "read_file", VALID_ARGS, LONG_RESPONSE)
        assert "cached memory" in out2
        assert pipeline.search.call_count == 1  # not called again
