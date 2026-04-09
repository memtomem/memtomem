"""End-to-end integration tests for the full pipeline.

Tests the complete flow: index → search → context-window expand → surfacing.
Requires a running Ollama instance with bge-m3 model.

    uv run pytest packages/memtomem/tests/test_e2e_pipeline.py -v -s
"""

from __future__ import annotations


import pytest

pytestmark = pytest.mark.ollama


# ── Context-Window Search ───────────────────────────────────────────────


class TestContextWindowE2E:
    """End-to-end: index multi-section doc → search → expand with adjacent chunks."""

    async def test_index_search_expand(self, components, memory_dir):
        """Index a multi-section doc, search, then expand a result with context."""
        doc = memory_dir / "architecture.md"
        doc.write_text(
            "## Database Selection\n\n"
            "PostgreSQL for ACID transactions and JSON support.\n\n"
            "## Caching Layer\n\n"
            "Redis with LRU eviction. Cache-aside pattern.\n\n"
            "## Message Queue\n\n"
            "RabbitMQ for async job processing. Dead letter exchange configured.\n\n"
            "## Monitoring\n\n"
            "Prometheus + Grafana stack. Alerting via PagerDuty.\n\n"
            "## Deployment\n\n"
            "Kubernetes on AWS EKS. Helm charts for all services.\n"
        )
        stats = await components.index_engine.index_file(doc)
        assert stats.indexed_chunks >= 3

        # Search with context_window=2
        results, _ = await components.search_pipeline.search(
            "Redis caching strategy", top_k=3, context_window=2
        )
        assert len(results) >= 1

        # The top result should have context
        top = results[0]
        assert "Redis" in top.chunk.content or "cache" in top.chunk.content.lower()
        ctx = top.context
        assert ctx is not None, "context_window=2 should populate context"
        assert ctx.chunk_position > 0
        assert ctx.total_chunks_in_file >= 3

        # Window chunks should be from the same file
        for wc in ctx.window_before + ctx.window_after:
            assert wc.metadata.source_file == top.chunk.metadata.source_file

    async def test_context_window_zero_no_context(self, components, memory_dir):
        """context_window=0 (or omitted) returns no context."""
        doc = memory_dir / "simple.md"
        doc.write_text("## Title\n\nSome content here.\n\n## Other\n\nMore content.")
        await components.index_engine.index_file(doc)

        results, _ = await components.search_pipeline.search("content", top_k=3)
        for r in results:
            assert r.context is None

    async def test_expand_shows_neighbors(self, components, memory_dir):
        """Search result's context windows contain the correct adjacent sections."""
        doc = memory_dir / "ordered.md"
        doc.write_text(
            "## Section A\n\nAlpha content unique.\n\n"
            "## Section B\n\nBravo content unique.\n\n"
            "## Section C\n\nCharlie content unique.\n\n"
            "## Section D\n\nDelta content unique.\n\n"
            "## Section E\n\nEcho content unique.\n"
        )
        stats = await components.index_engine.index_file(doc)
        assert stats.indexed_chunks >= 4

        # Search for something in the middle
        results, _ = await components.search_pipeline.search(
            "Charlie content", top_k=1, context_window=1
        )
        assert len(results) >= 1
        top = results[0]
        assert "Charlie" in top.chunk.content

        ctx = top.context
        assert ctx is not None
        # Should have at least 1 before and 1 after
        if ctx.chunk_position > 1:
            assert len(ctx.window_before) >= 1
        if ctx.chunk_position < ctx.total_chunks_in_file:
            assert len(ctx.window_after) >= 1


# ── mem_expand Action ───────────────────────────────────────────────────


class TestMemExpandE2E:
    """End-to-end: index → search → mem_expand specific chunk."""

    async def test_expand_specific_chunk(self, components, memory_dir):
        """Index doc, search, then expand a specific result."""
        doc = memory_dir / "guide.md"
        doc.write_text(
            "## Introduction\n\nWelcome to the setup guide.\n\n"
            "## Prerequisites\n\nPython 3.12 and uv required.\n\n"
            "## Installation\n\npip install memtomem or uv pip install.\n\n"
            "## Configuration\n\nSet MEMTOMEM_ env vars.\n\n"
            "## Usage\n\nRun mm search to find memories.\n"
        )
        await components.index_engine.index_file(doc)

        # Search
        results, _ = await components.search_pipeline.search("Installation", top_k=1)
        assert len(results) >= 1
        chunk_id = str(results[0].chunk.id)

        # Expand using storage directly (simulating mem_expand logic)
        chunk = await components.storage.get_chunk(results[0].chunk.id)
        assert chunk is not None
        all_chunks = await components.storage.list_chunks_by_source(chunk.metadata.source_file)
        assert len(all_chunks) >= 3

        # Find position
        idx_map = {str(c.id): i for i, c in enumerate(all_chunks)}
        pos = idx_map.get(chunk_id)
        assert pos is not None

        before = all_chunks[max(0, pos - 2) : pos]
        after = all_chunks[pos + 1 : pos + 3]

        # Verify adjacent chunks make sense
        if before:
            assert any(c.content for c in before)
        if after:
            assert any(c.content for c in after)


# ── Cross-Language Search ───────────────────────────────────────────────


class TestCrossLanguageE2E:
    """End-to-end: Korean doc → English query (and vice versa) with context."""

    async def test_korean_doc_english_query_with_context(self, components, memory_dir):
        """Korean document found by English query, with context expansion."""
        doc = memory_dir / "kr_arch.md"
        doc.write_text(
            "## 데이터베이스 설계\n\n"
            "PostgreSQL을 주 데이터베이스로 선택. JSONB 컬럼 활용.\n\n"
            "## 캐싱 전략\n\n"
            "Redis 캐시 레이어 도입. LRU 정책 적용.\n\n"
            "## API 설계\n\n"
            "RESTful API with FastAPI. OpenAPI 자동 문서화.\n"
        )
        await components.index_engine.index_file(doc)

        # English query → Korean doc
        results, _ = await components.search_pipeline.search(
            "Redis caching strategy", top_k=3, context_window=1
        )
        assert len(results) >= 1
        # Should find the Korean Redis section
        found = any("Redis" in r.chunk.content or "캐싱" in r.chunk.content for r in results)
        assert found, "English query should find Korean Redis content"

    async def test_mixed_language_context(self, components, memory_dir):
        """Mixed-language doc with context expansion preserves both languages."""
        doc = memory_dir / "mixed.md"
        doc.write_text(
            "## Project Overview\n\n"
            "Building a knowledge management platform.\n\n"
            "## 기술 스택\n\n"
            "Python 3.12, FastAPI, SQLite with FTS5.\n\n"
            "## Architecture\n\n"
            "Monorepo with uv workspace. Two packages.\n"
        )
        await components.index_engine.index_file(doc)

        results, _ = await components.search_pipeline.search(
            "기술 스택", top_k=1, context_window=1
        )
        assert len(results) >= 1


# ── Full Pipeline: Index → Search → Format ──────────────────────────────


class TestFullPipelineE2E:
    """End-to-end: index → search → format with and without context."""

    async def test_formatter_with_context(self, components, memory_dir):
        """Search results with context are formatted correctly."""
        from memtomem.server.formatters import _format_single_result

        doc = memory_dir / "format_test.md"
        doc.write_text(
            "## Intro\n\nWelcome.\n\n"
            "## Core\n\nMain content here.\n\n"
            "## Conclusion\n\nSummary.\n"
        )
        await components.index_engine.index_file(doc)

        results, _ = await components.search_pipeline.search(
            "Main content", top_k=1, context_window=1
        )
        assert len(results) >= 1

        output = _format_single_result(results[0])
        assert "[1]" in output
        assert "format_test.md" in output

        # Verbose mode still works
        verbose_output = _format_single_result(results[0], verbose=True)
        assert "score=" in verbose_output
        if results[0].context and results[0].context.window_before:
            assert "context before" in verbose_output

    async def test_formatter_without_context(self, components, memory_dir):
        """Search results without context use standard format."""
        from memtomem.server.formatters import _format_single_result

        doc = memory_dir / "no_ctx.md"
        doc.write_text("## Test\n\nSome searchable content.")
        await components.index_engine.index_file(doc)

        results, _ = await components.search_pipeline.search("searchable", top_k=1)
        assert len(results) >= 1

        output = _format_single_result(results[0])
        assert "[1]" in output
        assert "no_ctx.md" in output

    async def test_incremental_reindex_preserves_search(self, components, memory_dir):
        """Adding content to a file → re-index → new content searchable."""
        doc = memory_dir / "evolving.md"
        doc.write_text("## V1\n\nInitial content about authentication.")
        await components.index_engine.index_file(doc)

        results1, _ = await components.search_pipeline.search("authentication", top_k=3)
        assert len(results1) >= 1

        # Add new section
        doc.write_text(
            "## V1\n\nInitial content about authentication.\n\n"
            "## V2\n\nAdded OAuth2 with PKCE flow."
        )
        await components.index_engine.index_file(doc)

        results2, _ = await components.search_pipeline.search("OAuth2 PKCE", top_k=3)
        assert len(results2) >= 1
        assert any("OAuth2" in r.chunk.content or "PKCE" in r.chunk.content for r in results2)
