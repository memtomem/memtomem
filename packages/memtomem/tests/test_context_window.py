"""Tests for context-window search (small-to-big retrieval)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from memtomem.config import ContextWindowConfig, SearchConfig
from memtomem.models import Chunk, ChunkMetadata, ContextInfo, SearchResult
from memtomem.search.pipeline import SearchPipeline
from memtomem.server.formatters import _format_single_result


# ── Helpers ──────────────────────────────────────────────────────────────


def _make_chunk(
    content: str,
    source: str = "/tmp/doc.md",
    start_line: int = 0,
    end_line: int = 10,
    heading: tuple[str, ...] = (),
    chunk_id: UUID | None = None,
) -> Chunk:
    return Chunk(
        content=content,
        metadata=ChunkMetadata(
            source_file=Path(source),
            start_line=start_line,
            end_line=end_line,
            heading_hierarchy=heading,
        ),
        id=chunk_id or uuid4(),
        content_hash=f"hash-{uuid4().hex[:8]}",
        embedding=[0.1] * 768,
    )


def _make_file_chunks(source: str, count: int) -> list[Chunk]:
    """Create N ordered chunks for a single source file."""
    return [
        _make_chunk(
            content=f"Content of chunk {i} in {source}",
            source=source,
            start_line=i * 10,
            end_line=i * 10 + 9,
            heading=(f"Section {i}",),
        )
        for i in range(count)
    ]


def _make_result(chunk: Chunk, score: float = 0.8, rank: int = 1) -> SearchResult:
    return SearchResult(chunk=chunk, score=score, rank=rank, source="fused")


def _make_pipeline(
    chunks_by_source: dict[Path, list[Chunk]],
    bm25_results: list[SearchResult] | None = None,
    context_window_config: ContextWindowConfig | None = None,
) -> SearchPipeline:
    """Create a pipeline with mocked storage and embedder."""
    storage = AsyncMock()
    storage.list_chunks_by_sources = AsyncMock(return_value=chunks_by_source)
    storage.bm25_search = AsyncMock(return_value=bm25_results or [])
    storage.dense_search = AsyncMock(return_value=[])
    storage.increment_access = AsyncMock()
    storage.save_query_history = AsyncMock()
    storage.get_access_counts = AsyncMock(return_value={})
    storage.get_embeddings_for_chunks = AsyncMock(return_value={})
    storage.get_importance_scores = AsyncMock(return_value={})

    embedder = AsyncMock()
    embedder.embed_query = AsyncMock(return_value=[0.1] * 768)

    config = SearchConfig(enable_bm25=True, enable_dense=False)

    return SearchPipeline(
        storage=storage,
        embedder=embedder,
        config=config,
        context_window_config=context_window_config,
    )


# ── _expand_context unit tests ──────────────────────────────────────────


class TestExpandContext:
    async def test_basic_expansion_middle_chunk(self):
        """Middle chunk (pos 2/5) with window=2 gets 2 before, 2 after."""
        chunks = _make_file_chunks("/tmp/doc.md", 5)
        target = chunks[2]

        results = [_make_result(target)]
        pipeline = _make_pipeline({Path("/tmp/doc.md"): chunks})

        expanded = await pipeline._expand_context(results, window=2)

        assert len(expanded) == 1
        ctx = expanded[0].context
        assert ctx is not None
        assert len(ctx.window_before) == 2
        assert len(ctx.window_after) == 2
        assert ctx.chunk_position == 3
        assert ctx.total_chunks_in_file == 5
        assert ctx.context_tier_used == "standard"
        # Verify order
        assert ctx.window_before[0].content == chunks[0].content
        assert ctx.window_before[1].content == chunks[1].content
        assert ctx.window_after[0].content == chunks[3].content
        assert ctx.window_after[1].content == chunks[4].content

    async def test_first_chunk_no_before(self):
        """First chunk has no before, only after."""
        chunks = _make_file_chunks("/tmp/doc.md", 5)
        results = [_make_result(chunks[0])]
        pipeline = _make_pipeline({Path("/tmp/doc.md"): chunks})

        expanded = await pipeline._expand_context(results, window=2)
        ctx = expanded[0].context
        assert ctx is not None
        assert len(ctx.window_before) == 0
        assert len(ctx.window_after) == 2
        assert ctx.chunk_position == 1

    async def test_last_chunk_no_after(self):
        """Last chunk has before, no after."""
        chunks = _make_file_chunks("/tmp/doc.md", 5)
        results = [_make_result(chunks[4])]
        pipeline = _make_pipeline({Path("/tmp/doc.md"): chunks})

        expanded = await pipeline._expand_context(results, window=2)
        ctx = expanded[0].context
        assert ctx is not None
        assert len(ctx.window_before) == 2
        assert len(ctx.window_after) == 0
        assert ctx.chunk_position == 5

    async def test_same_file_multiple_results(self):
        """Two results from same file: batch fetch called once."""
        chunks = _make_file_chunks("/tmp/doc.md", 5)
        results = [_make_result(chunks[1], rank=1), _make_result(chunks[3], rank=2)]
        pipeline = _make_pipeline({Path("/tmp/doc.md"): chunks})

        expanded = await pipeline._expand_context(results, window=1)

        assert len(expanded) == 2
        # chunk[1]: before=[0], after=[2]
        assert len(expanded[0].context.window_before) == 1
        assert len(expanded[0].context.window_after) == 1
        # chunk[3]: before=[2], after=[4]
        assert len(expanded[1].context.window_before) == 1
        assert len(expanded[1].context.window_after) == 1

        # Verify storage was called once (batch)
        pipeline._storage.list_chunks_by_sources.assert_called_once()

    async def test_multiple_source_files(self):
        """Results from different files get correct context."""
        chunks_a = _make_file_chunks("/tmp/a.md", 3)
        chunks_b = _make_file_chunks("/tmp/b.md", 4)

        results = [_make_result(chunks_a[1], rank=1), _make_result(chunks_b[2], rank=2)]
        pipeline = _make_pipeline(
            {
                Path("/tmp/a.md"): chunks_a,
                Path("/tmp/b.md"): chunks_b,
            }
        )

        expanded = await pipeline._expand_context(results, window=1)

        assert expanded[0].context.total_chunks_in_file == 3
        assert expanded[1].context.total_chunks_in_file == 4

    async def test_window_zero_no_expansion(self):
        """window=0 returns results unchanged (no context)."""
        chunks = _make_file_chunks("/tmp/doc.md", 5)
        results = [_make_result(chunks[2])]
        pipeline = _make_pipeline({})

        expanded = await pipeline._expand_context(results, window=0)
        assert expanded[0].context is None

    async def test_deleted_chunk_graceful(self):
        """Chunk ID not in source listing → context stays None."""
        chunks = _make_file_chunks("/tmp/doc.md", 3)
        orphan = _make_chunk("orphan", source="/tmp/doc.md", start_line=100)
        results = [_make_result(orphan)]
        pipeline = _make_pipeline({Path("/tmp/doc.md"): chunks})

        expanded = await pipeline._expand_context(results, window=2)
        assert expanded[0].context is None

    async def test_config_disabled(self):
        """When config.enabled=False, resolve_context_window returns 0."""
        cfg = ContextWindowConfig(enabled=False, window_size=2)
        chunks = _make_file_chunks("/tmp/doc.md", 5)
        bm25_results = [_make_result(chunks[2])]
        pipeline = _make_pipeline(
            {Path("/tmp/doc.md"): chunks},
            bm25_results=bm25_results,
            context_window_config=cfg,
        )

        results, _ = await pipeline.search("test")
        assert results[0].context is None


# ── Per-call override ───────────────────────────────────────────────────


class TestPerCallOverride:
    async def test_override_enables_expansion(self):
        """context_window=3 overrides config disabled."""
        chunks = _make_file_chunks("/tmp/doc.md", 7)
        bm25_results = [_make_result(chunks[3])]
        pipeline = _make_pipeline(
            {Path("/tmp/doc.md"): chunks},
            bm25_results=bm25_results,
            context_window_config=None,  # no config
        )

        results, _ = await pipeline.search("test", context_window=3)
        ctx = results[0].context
        assert ctx is not None
        assert len(ctx.window_before) == 3
        assert len(ctx.window_after) == 3

    async def test_cache_key_differs(self):
        """Different context_window values produce different cache keys."""
        pipeline = _make_pipeline({})
        key0 = pipeline._cache_key("q", 10, None, None, None, context_window=0)
        key2 = pipeline._cache_key("q", 10, None, None, None, context_window=2)
        assert key0 != key2


# ── mem_expand action tests ─────────────────────────────────────────────


class TestMemExpand:
    async def test_expand_basic(self):
        """mem_expand returns before/after context."""
        from memtomem.server.tools.search import mem_expand

        chunks = _make_file_chunks("/tmp/doc.md", 5)
        target = chunks[2]

        app = MagicMock()
        app.storage.get_chunk = AsyncMock(return_value=target)
        app.storage.list_chunks_by_source = AsyncMock(return_value=chunks)

        # Mock ctx
        ctx = SimpleNamespace()

        with pytest.MonkeyPatch.context() as m:
            m.setattr("memtomem.server.tools.search._get_app", lambda _: app)
            result = await mem_expand(chunk_id=str(target.id), window=2, ctx=ctx)

        assert "chunk 3/5" in result
        assert "Before" in result
        assert "After" in result
        assert "Matched" in result

    async def test_expand_first_chunk(self):
        """First chunk has no Before section."""
        from memtomem.server.tools.search import mem_expand

        chunks = _make_file_chunks("/tmp/doc.md", 3)
        target = chunks[0]

        app = MagicMock()
        app.storage.get_chunk = AsyncMock(return_value=target)
        app.storage.list_chunks_by_source = AsyncMock(return_value=chunks)

        ctx = SimpleNamespace()
        with pytest.MonkeyPatch.context() as m:
            m.setattr("memtomem.server.tools.search._get_app", lambda _: app)
            result = await mem_expand(chunk_id=str(target.id), window=2, ctx=ctx)

        assert "Before" not in result
        assert "After" in result

    async def test_expand_not_found(self):
        """Invalid chunk_id returns error."""
        from memtomem.server.tools.search import mem_expand

        app = MagicMock()
        app.storage.get_chunk = AsyncMock(return_value=None)

        ctx = SimpleNamespace()
        with pytest.MonkeyPatch.context() as m:
            m.setattr("memtomem.server.tools.search._get_app", lambda _: app)
            result = await mem_expand(chunk_id=str(uuid4()), window=2, ctx=ctx)

        assert "not found" in result


# ── mem_increment_access action tests ──────────────────────────────────


class TestMemIncrementAccess:
    """Tests for mem_increment_access — used by external surfacing systems
    (e.g. memtomem-stm) to record positive feedback as a search-ranking boost.
    """

    async def test_action_registered_in_search_category(self):
        """The action should be discoverable via the mem_do registry."""
        from memtomem.server.tool_registry import ACTIONS

        assert "increment_access" in ACTIONS
        assert ACTIONS["increment_access"].category == "search"
        assert "chunk_ids" in ACTIONS["increment_access"].params

    async def test_empty_chunk_ids_returns_message(self):
        """Empty list short-circuits with a friendly message — no storage call."""
        from memtomem.server.tools.search import mem_increment_access

        app = MagicMock()
        app.storage.increment_access = AsyncMock()

        with pytest.MonkeyPatch.context() as m:
            m.setattr("memtomem.server.tools.search._get_app", lambda _: app)
            result = await mem_increment_access(chunk_ids=[], ctx=SimpleNamespace())

        assert "No chunk_ids" in result
        app.storage.increment_access.assert_not_called()

    async def test_all_invalid_uuids_rejected(self):
        """All-invalid input returns error and never touches storage."""
        from memtomem.server.tools.search import mem_increment_access

        app = MagicMock()
        app.storage.increment_access = AsyncMock()

        with pytest.MonkeyPatch.context() as m:
            m.setattr("memtomem.server.tools.search._get_app", lambda _: app)
            result = await mem_increment_access(
                chunk_ids=["not-a-uuid", "also-bad"],
                ctx=SimpleNamespace(),
            )

        assert "no valid UUIDs" in result
        assert "rejected: 2" in result
        app.storage.increment_access.assert_not_called()

    async def test_valid_uuids_increments_storage(self):
        """Valid UUIDs are converted and forwarded to storage.increment_access."""
        from memtomem.server.tools.search import mem_increment_access

        app = MagicMock()
        app.storage.increment_access = AsyncMock()

        ids = [str(uuid4()), str(uuid4()), str(uuid4())]
        with pytest.MonkeyPatch.context() as m:
            m.setattr("memtomem.server.tools.search._get_app", lambda _: app)
            result = await mem_increment_access(chunk_ids=ids, ctx=SimpleNamespace())

        assert "3 chunk(s)" in result
        app.storage.increment_access.assert_awaited_once()
        called_ids = app.storage.increment_access.call_args.args[0]
        assert len(called_ids) == 3
        assert all(isinstance(c, UUID) for c in called_ids)

    async def test_mixed_valid_invalid_partial_increment(self):
        """Mixed input increments the valid ones and reports the skipped count."""
        from memtomem.server.tools.search import mem_increment_access

        app = MagicMock()
        app.storage.increment_access = AsyncMock()

        valid_id = str(uuid4())
        with pytest.MonkeyPatch.context() as m:
            m.setattr("memtomem.server.tools.search._get_app", lambda _: app)
            result = await mem_increment_access(
                chunk_ids=[valid_id, "not-a-uuid"],
                ctx=SimpleNamespace(),
            )

        assert "1 chunk(s)" in result
        assert "Skipped 1 invalid" in result
        app.storage.increment_access.assert_awaited_once()
        called_ids = app.storage.increment_access.call_args.args[0]
        assert len(called_ids) == 1


# ── Formatter tests ─────────────────────────────────────────────────────


class TestCoreFormatter:
    def test_with_context_compact(self):
        """Compact formatter shows before/after content inline."""
        chunk = _make_chunk("matched content", heading=("Intro",))
        before = _make_chunk("before content")
        after = _make_chunk("after content")
        ctx = ContextInfo(
            window_before=(before,),
            window_after=(after,),
            chunk_position=2,
            total_chunks_in_file=3,
            context_tier_used="standard",
        )
        r = SearchResult(chunk=chunk, score=0.85, rank=1, source="fused", context=ctx)
        output = _format_single_result(r)

        assert "[2/3]" in output
        assert "before content" in output
        assert "after content" in output
        assert "matched content" in output

    def test_with_context_verbose(self):
        """Verbose formatter shows labeled sections with code blocks."""
        chunk = _make_chunk("matched content", heading=("Intro",))
        before = _make_chunk("before content")
        after = _make_chunk("after content")
        ctx = ContextInfo(
            window_before=(before,),
            window_after=(after,),
            chunk_position=2,
            total_chunks_in_file=3,
            context_tier_used="standard",
        )
        r = SearchResult(chunk=chunk, score=0.85, rank=1, source="fused", context=ctx)
        output = _format_single_result(r, verbose=True)

        assert "[chunk 2/3]" in output
        assert "context before" in output
        assert "context after" in output
        assert "matched" in output

    def test_without_context(self):
        """Formatter uses standard format when no context."""
        chunk = _make_chunk("just content")
        r = SearchResult(chunk=chunk, score=0.85, rank=1, source="fused")
        output = _format_single_result(r)

        assert "just content" in output


# NOTE: TestSTMFormatter (which exercised SurfacingFormatter from
# memtomem_stm) was removed when STM code was decoupled from core. Those
# context-window assertions live with the STM package tests now and should
# not be re-added here — core tests must not import memtomem_stm.


# ── Integration: pipeline search() ──────────────────────────────────────


class TestPipelineIntegration:
    async def test_search_with_context_window(self):
        """Full pipeline search() returns results with context populated."""
        chunks = _make_file_chunks("/tmp/doc.md", 5)
        bm25_results = [
            SearchResult(chunk=chunks[2], score=0.9, rank=1, source="bm25"),
        ]
        cfg = ContextWindowConfig(enabled=True, window_size=2)
        pipeline = _make_pipeline(
            {Path("/tmp/doc.md"): chunks},
            bm25_results=bm25_results,
            context_window_config=cfg,
        )

        results, stats = await pipeline.search("test")

        assert len(results) == 1
        ctx = results[0].context
        assert ctx is not None
        assert len(ctx.window_before) == 2
        assert len(ctx.window_after) == 2
        assert ctx.chunk_position == 3
        assert ctx.total_chunks_in_file == 5
