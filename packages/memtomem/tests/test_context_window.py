"""Tests for context-window search (small-to-big retrieval)."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from memtomem.config import ContextWindowConfig, SearchConfig
from memtomem.models import Chunk, ChunkMetadata, ChunkType, ContextInfo, SearchResult
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
    namespace: str = "default",
    scope: str = "user",
    project_root: Path | None = None,
    valid_from_unix: int | None = None,
    valid_to_unix: int | None = None,
) -> Chunk:
    return Chunk(
        content=content,
        metadata=ChunkMetadata(
            source_file=Path(source),
            start_line=start_line,
            end_line=end_line,
            heading_hierarchy=heading,
            namespace=namespace,
            scope=scope,
            project_root=project_root,
            valid_from_unix=valid_from_unix,
            valid_to_unix=valid_to_unix,
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
    search_config: SearchConfig | None = None,
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

    config = search_config or SearchConfig(enable_bm25=True, enable_dense=False)

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
            m.setattr(
                "memtomem.server.tools.search._get_app_initialized", AsyncMock(return_value=app)
            )
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
            m.setattr(
                "memtomem.server.tools.search._get_app_initialized", AsyncMock(return_value=app)
            )
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
            m.setattr(
                "memtomem.server.tools.search._get_app_initialized", AsyncMock(return_value=app)
            )
            result = await mem_expand(chunk_id=str(uuid4()), window=2, ctx=ctx)

        assert "not found" in result

    async def _expand_mixed(self, anchor_index: int, monkeypatch, prefixes=None):
        """Run mem_expand over the mixed-namespace file, anchored at N."""
        from memtomem.server.tools.search import mem_expand

        chunks = _mixed_namespace_file()
        app = MagicMock()
        app.config.search.system_namespace_prefixes = (
            ["archive:", "agent-runtime:"] if prefixes is None else prefixes
        )
        app.storage.get_chunk = AsyncMock(return_value=chunks[anchor_index])
        app.storage.list_chunks_by_source = AsyncMock(return_value=chunks)

        monkeypatch.setattr(
            "memtomem.server.tools.search._get_app_initialized", AsyncMock(return_value=app)
        )
        monkeypatch.setattr(
            "memtomem.server.tools.search._resolve_project_context_root", lambda _app: None
        )
        result = await mem_expand(chunk_id=str(chunks[anchor_index].id), window=2, ctx=None)
        return chunks, result

    async def test_expand_hides_system_namespace_neighbors(self, monkeypatch):
        """#2192: the id-addressed path enforces the same visibility rule."""
        chunks, result = await self._expand_mixed(2, monkeypatch)

        assert chunks[0].content in result
        assert chunks[4].content in result
        assert chunks[1].content not in result
        assert chunks[3].content not in result
        # Positions count only what the caller may see.
        assert "chunk 2/3" in result

    async def test_expand_from_hidden_anchor_gets_no_hidden_context(self, monkeypatch):
        """#2236: the id is not an opt-in into its own namespace.

        Possessing an id does not imply the caller was allowed to see the
        chunk — `mem_dedup_scan`, `mem_export` and the web `/chunks` route
        all hand out ids for chunks the default rules hide — so the anchor's
        namespace cannot be read as consent to its neighbours.
        """
        chunks = _mixed_namespace_file()
        chunks.insert(
            2,
            _make_chunk(
                "sibling archive",
                source="/tmp/mixed.md",
                start_line=15,
                namespace="archive:2024",
            ),
        )
        from memtomem.server.tools.search import mem_expand

        app = MagicMock()
        app.config.search.system_namespace_prefixes = ["archive:", "agent-runtime:"]
        app.storage.get_chunk = AsyncMock(return_value=chunks[1])
        app.storage.list_chunks_by_source = AsyncMock(return_value=chunks)
        monkeypatch.setattr(
            "memtomem.server.tools.search._get_app_initialized", AsyncMock(return_value=app)
        )
        monkeypatch.setattr(
            "memtomem.server.tools.search._resolve_project_context_root", lambda _app: None
        )

        result = await mem_expand(chunk_id=str(chunks[1].id), window=2, ctx=None)

        # The addressed chunk is still returned — that read is mem_read's
        # contract, unchanged here — but nothing hidden comes with it.
        assert "sibling archive" not in result
        assert "hidden after" not in result
        # The one visible neighbour in range is still context.
        assert chunks[0].content in result

    async def test_expand_does_not_reach_into_the_anchors_project(self, monkeypatch):
        """#2236: nor is the anchor's project root an opt-in.

        Out of project context, the ADR-0011 boundary admits ``user`` rows
        only, whatever project the addressed chunk belongs to.

        ADR-0036 carried that one step further: the *anchor* is screened by
        the same boundary too, so the case this test was written for — a
        project-tier chunk addressed from outside its project — now stops at
        the anchor and never reaches the neighbour rule. Pinned here in that
        stronger form; the neighbour half moved to
        ``test_expand_in_context_still_hides_other_projects``, which keeps it
        measurable with an anchor that does resolve.
        """
        from memtomem.server.tools.search import mem_expand

        chunks = [
            _make_chunk(
                "foreign project",
                source="/tmp/s.md",
                start_line=0,
                scope="project_shared",
                project_root=Path("/elsewhere"),
            ),
            _make_chunk(
                "anchor",
                source="/tmp/s.md",
                start_line=10,
                scope="project_shared",
                project_root=Path("/mine"),
            ),
            _make_chunk(
                "same project",
                source="/tmp/s.md",
                start_line=20,
                scope="project_shared",
                project_root=Path("/mine"),
            ),
            _make_chunk("user row", source="/tmp/s.md", start_line=30),
        ]
        app = MagicMock()
        app.config.search.system_namespace_prefixes = []
        app.storage.get_chunk = AsyncMock(return_value=chunks[1])
        app.storage.list_chunks_by_source = AsyncMock(return_value=chunks)
        monkeypatch.setattr(
            "memtomem.server.tools.search._get_app_initialized", AsyncMock(return_value=app)
        )
        monkeypatch.setattr(
            "memtomem.server.tools.search._resolve_project_context_root", lambda _app: None
        )

        result = await mem_expand(chunk_id=str(chunks[1].id), window=2, ctx=None)

        # The anchor itself does not resolve, so nothing around it is reached
        # — and the answer is the one a nonexistent id gets.
        assert result == f"Chunk {chunks[1].id} not found."

    async def test_expand_in_context_still_hides_other_projects(self, monkeypatch):
        """In-project, the boundary keeps a neighbour from another project out.

        The other half of the rule above, with an anchor that resolves: a
        caller working in ``/mine`` expands one of its chunks and sees its own
        project's rows and ``user`` rows, never ``/elsewhere``'s — even though
        all of them share one source file.
        """
        from memtomem.server.tools.search import mem_expand

        chunks = [
            _make_chunk(
                "foreign project",
                source="/tmp/s.md",
                start_line=0,
                scope="project_shared",
                project_root=Path("/elsewhere"),
            ),
            _make_chunk(
                "anchor",
                source="/tmp/s.md",
                start_line=10,
                scope="project_shared",
                project_root=Path("/mine"),
            ),
            _make_chunk(
                "same project",
                source="/tmp/s.md",
                start_line=20,
                scope="project_shared",
                project_root=Path("/mine"),
            ),
            _make_chunk("user row", source="/tmp/s.md", start_line=30),
        ]
        app = MagicMock()
        app.config.search.system_namespace_prefixes = []
        app.storage.get_chunk = AsyncMock(return_value=chunks[1])
        app.storage.list_chunks_by_source = AsyncMock(return_value=chunks)
        monkeypatch.setattr(
            "memtomem.server.tools.search._get_app_initialized", AsyncMock(return_value=app)
        )
        monkeypatch.setattr(
            "memtomem.server.tools.search._resolve_project_context_root",
            lambda _app: Path("/mine"),
        )

        result = await mem_expand(chunk_id=str(chunks[1].id), window=2, ctx=None)

        assert "anchor" in result
        assert "same project" in result
        assert "user row" in result
        assert "foreign project" not in result

    async def test_expand_counts_an_expired_anchor_in_its_own_position(self, monkeypatch):
        """The named chunk is returned, so it must be part of the accounting."""
        from memtomem.server.tools.search import mem_expand

        chunks = [
            _make_chunk("live before", source="/tmp/v.md", start_line=0),
            _make_chunk("anchor", source="/tmp/v.md", start_line=10, valid_to_unix=1_000),
            _make_chunk("live after", source="/tmp/v.md", start_line=20),
        ]
        app = MagicMock()
        app.config.search.system_namespace_prefixes = []
        app.storage.get_chunk = AsyncMock(return_value=chunks[1])
        app.storage.list_chunks_by_source = AsyncMock(return_value=chunks)
        monkeypatch.setattr(
            "memtomem.server.tools.search._get_app_initialized", AsyncMock(return_value=app)
        )
        monkeypatch.setattr(
            "memtomem.server.tools.search._resolve_project_context_root", lambda _app: None
        )

        result = await mem_expand(chunk_id=str(chunks[1].id), window=2, ctx=None)

        assert "chunk 2/3" in result

    async def test_expand_drops_expired_neighbor(self, monkeypatch):
        from memtomem.server.tools.search import mem_expand

        chunks = [
            _make_chunk("expired", source="/tmp/v.md", start_line=0, valid_to_unix=1_000),
            _make_chunk("anchor", source="/tmp/v.md", start_line=10),
            _make_chunk("live", source="/tmp/v.md", start_line=20),
        ]
        app = MagicMock()
        app.config.search.system_namespace_prefixes = []
        app.storage.get_chunk = AsyncMock(return_value=chunks[1])
        app.storage.list_chunks_by_source = AsyncMock(return_value=chunks)
        monkeypatch.setattr(
            "memtomem.server.tools.search._get_app_initialized", AsyncMock(return_value=app)
        )
        monkeypatch.setattr(
            "memtomem.server.tools.search._resolve_project_context_root", lambda _app: None
        )

        result = await mem_expand(chunk_id=str(chunks[1].id), window=2, ctx=None)

        assert "expired" not in result
        assert "live" in result


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
            m.setattr(
                "memtomem.server.tools.search._get_app_initialized", AsyncMock(return_value=app)
            )
            result = await mem_increment_access(chunk_ids=[], ctx=SimpleNamespace())

        assert "No chunk_ids" in result
        app.storage.increment_access.assert_not_called()

    async def test_all_invalid_uuids_rejected(self):
        """All-invalid input returns error and never touches storage."""
        from memtomem.server.tools.search import mem_increment_access

        app = MagicMock()
        app.storage.increment_access = AsyncMock()

        with pytest.MonkeyPatch.context() as m:
            m.setattr(
                "memtomem.server.tools.search._get_app_initialized", AsyncMock(return_value=app)
            )
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
        # Ids are screened before the boost lands (ADR-0036), so the mock
        # has to answer the lookup with an in-boundary chunk.
        ids = [str(uuid4()), str(uuid4()), str(uuid4())]
        app.storage.get_chunks_batch = AsyncMock(
            return_value={UUID(i): _make_chunk("in boundary") for i in ids}
        )
        with pytest.MonkeyPatch.context() as m:
            m.setattr(
                "memtomem.server.tools.search._get_app_initialized", AsyncMock(return_value=app)
            )
            m.setattr(
                "memtomem.server.tools.search._resolve_project_context_root", lambda _app: None
            )
            result = await mem_increment_access(chunk_ids=ids, ctx=SimpleNamespace())

        assert "Accepted 3 chunk id(s)" in result
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
        app.storage.get_chunks_batch = AsyncMock(
            return_value={UUID(valid_id): _make_chunk("in boundary")}
        )
        with pytest.MonkeyPatch.context() as m:
            m.setattr(
                "memtomem.server.tools.search._get_app_initialized", AsyncMock(return_value=app)
            )
            m.setattr(
                "memtomem.server.tools.search._resolve_project_context_root", lambda _app: None
            )
            result = await mem_increment_access(
                chunk_ids=[valid_id, "not-a-uuid"],
                ctx=SimpleNamespace(),
            )

        assert "Accepted 1 chunk id(s)" in result
        assert "Skipped 1 invalid" in result
        app.storage.increment_access.assert_awaited_once()
        called_ids = app.storage.increment_access.call_args.args[0]
        assert len(called_ids) == 1

    async def test_out_of_boundary_ids_are_not_boosted(self):
        """ADR-0036: raising a foreign chunk's ranking is a write to it.

        The reported count stays the number of ids accepted for submission,
        so the response does not reveal which of them existed elsewhere.
        """
        from memtomem.server.tools.search import mem_increment_access

        app = MagicMock()
        app.storage.increment_access = AsyncMock()
        foreign_id = uuid4()
        app.storage.get_chunks_batch = AsyncMock(
            return_value={
                foreign_id: _make_chunk(
                    "another project's note",
                    scope="project_shared",
                    project_root=Path("/elsewhere"),
                )
            }
        )

        with pytest.MonkeyPatch.context() as m:
            m.setattr(
                "memtomem.server.tools.search._get_app_initialized", AsyncMock(return_value=app)
            )
            m.setattr(
                "memtomem.server.tools.search._resolve_project_context_root",
                lambda _app: Path("/mine"),
            )
            result = await mem_increment_access(chunk_ids=[str(foreign_id)], ctx=SimpleNamespace())

        assert "Accepted 1 chunk id(s)" in result
        app.storage.increment_access.assert_not_awaited()


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

    async def test_chunk_type_filter_keeps_different_type_context_neighbors(self):
        chunks = _make_file_chunks("/tmp/doc.md", 3)
        chunks[0].metadata = dataclasses.replace(chunks[0].metadata, chunk_type=ChunkType.RAW_TEXT)
        chunks[1].metadata = dataclasses.replace(
            chunks[1].metadata, chunk_type=ChunkType.MARKDOWN_SECTION
        )
        chunks[2].metadata = dataclasses.replace(chunks[2].metadata, chunk_type=ChunkType.RAW_TEXT)
        pipeline = _make_pipeline(
            {Path("/tmp/doc.md"): chunks},
            bm25_results=[SearchResult(chunk=chunks[1], score=0.9, rank=1, source="bm25")],
            context_window_config=ContextWindowConfig(enabled=True, window_size=1),
        )

        results, _ = await pipeline.search("test", chunk_types=[ChunkType.MARKDOWN_SECTION.value])

        assert len(results) == 1
        assert results[0].context is not None
        assert results[0].context.window_before == (chunks[0],)
        assert results[0].context.window_after == (chunks[2],)


# ── Neighbour visibility (#2192) ────────────────────────────────────────


def _mixed_namespace_file() -> list[Chunk]:
    """A source file whose chunks straddle the system-namespace boundary."""
    specs = [
        ("visible before", "default"),
        ("hidden before", "archive:2024"),
        ("anchor", "default"),
        ("hidden after", "agent-runtime:planner"),
        ("visible after", "default"),
    ]
    return [
        _make_chunk(
            content=content,
            source="/tmp/mixed.md",
            start_line=i * 10,
            end_line=i * 10 + 9,
            namespace=ns,
        )
        for i, (content, ns) in enumerate(specs)
    ]


def _pipeline_for(chunks, anchor, *, window=2, search_config=None):
    return _make_pipeline(
        {Path(str(chunks[0].metadata.source_file)): chunks},
        bm25_results=[SearchResult(chunk=anchor, score=0.9, rank=1, source="bm25")],
        context_window_config=ContextWindowConfig(enabled=True, window_size=window),
        search_config=search_config,
    )


class TestNeighborVisibility:
    """Neighbours inherit visibility filters, not selection filters (#2192)."""

    async def test_default_search_drops_system_namespace_neighbors(self):
        chunks = _mixed_namespace_file()
        pipeline = _pipeline_for(chunks, chunks[2])

        results, _ = await pipeline.search("test")

        ctx = results[0].context
        assert ctx is not None
        assert ctx.window_before == (chunks[0],)
        assert ctx.window_after == (chunks[4],)

    async def test_hidden_neighbor_shrinks_window_instead_of_backfilling(self):
        """Adjacency is physical: a hidden chunk is dropped, not replaced.

        With window=1 the only neighbours in range are the hidden ones, so
        both sides come back empty rather than reaching past them to the
        visible chunks two positions away.
        """
        chunks = _mixed_namespace_file()
        pipeline = _pipeline_for(chunks, chunks[2], window=1)

        results, _ = await pipeline.search("test")

        ctx = results[0].context
        assert ctx is not None
        assert ctx.window_before == ()
        assert ctx.window_after == ()

    async def test_position_and_total_count_visible_chunks_only(self):
        """Reporting the raw total would leak how many chunks are hidden."""
        chunks = _mixed_namespace_file()
        pipeline = _pipeline_for(chunks, chunks[2])

        results, _ = await pipeline.search("test")

        ctx = results[0].context
        assert ctx is not None
        assert ctx.total_chunks_in_file == 3
        assert ctx.chunk_position == 2

    async def test_explicit_namespace_surfaces_that_namespace_and_ordinary_ones(self):
        """Naming a namespace widens visibility; it does not narrow neighbours."""
        chunks = _mixed_namespace_file()
        pipeline = _pipeline_for(chunks, chunks[2])

        results, _ = await pipeline.search("test", namespace="archive:2024")

        ctx = results[0].context
        assert ctx is not None
        assert ctx.window_before == (chunks[0], chunks[1])
        # agent-runtime:planner was never asked for, so it stays hidden.
        assert ctx.window_after == (chunks[4],)

    async def test_namespace_glob_surfaces_matching_system_namespace(self):
        chunks = _mixed_namespace_file()
        pipeline = _pipeline_for(chunks, chunks[2])

        results, _ = await pipeline.search("test", namespace="archive:*")

        ctx = results[0].context
        assert ctx is not None
        assert chunks[1] in ctx.window_before

    async def test_empty_system_prefixes_hide_nothing(self):
        """``system_namespace_prefixes: []`` makes the parsed filter None."""
        chunks = _mixed_namespace_file()
        pipeline = _pipeline_for(
            chunks,
            chunks[2],
            search_config=SearchConfig(
                enable_bm25=True, enable_dense=False, system_namespace_prefixes=[]
            ),
        )

        results, _ = await pipeline.search("test")

        ctx = results[0].context
        assert ctx is not None
        assert ctx.window_before == (chunks[0], chunks[1])
        assert ctx.window_after == (chunks[3], chunks[4])

    async def test_expired_neighbor_is_dropped(self):
        chunks = [
            _make_chunk("before", source="/tmp/v.md", start_line=0, valid_to_unix=1_000),
            _make_chunk("anchor", source="/tmp/v.md", start_line=10),
            _make_chunk("after", source="/tmp/v.md", start_line=20),
        ]
        pipeline = _pipeline_for(chunks, chunks[1], window=1)

        results, _ = await pipeline.search("test", as_of_unix=5_000)

        ctx = results[0].context
        assert ctx is not None
        assert ctx.window_before == ()
        assert ctx.window_after == (chunks[2],)

    async def test_in_project_drops_other_projects_neighbors(self):
        chunks = [
            _make_chunk(
                "other project",
                source="/tmp/s.md",
                start_line=0,
                scope="project_shared",
                project_root=Path("/other"),
            ),
            _make_chunk("anchor", source="/tmp/s.md", start_line=10),
            _make_chunk(
                "this project",
                source="/tmp/s.md",
                start_line=20,
                scope="project_shared",
                project_root=Path("/proj"),
            ),
        ]
        pipeline = _pipeline_for(chunks, chunks[1], window=1)

        results, _ = await pipeline.search("test", project_context_root=Path("/proj"))

        ctx = results[0].context
        assert ctx is not None
        assert ctx.window_before == ()
        assert ctx.window_after == (chunks[2],)

    async def test_out_of_project_drops_project_tier_neighbors(self):
        chunks = [
            _make_chunk(
                "project row",
                source="/tmp/s.md",
                start_line=0,
                scope="project_local",
                project_root=Path("/proj"),
            ),
            _make_chunk("anchor", source="/tmp/s.md", start_line=10),
        ]
        pipeline = _pipeline_for(chunks, chunks[1], window=1)

        results, _ = await pipeline.search("test")

        ctx = results[0].context
        assert ctx is not None
        assert ctx.window_before == ()

    async def test_explicit_out_of_project_scope_opts_into_cross_project(self):
        """``scope=project_shared`` outside a project is the documented opt-in."""
        chunks = [
            _make_chunk(
                "other project",
                source="/tmp/s.md",
                start_line=0,
                scope="project_shared",
                project_root=Path("/other"),
            ),
            _make_chunk("anchor", source="/tmp/s.md", start_line=10),
            _make_chunk("user row", source="/tmp/s.md", start_line=20),
        ]
        pipeline = _pipeline_for(chunks, chunks[1], window=1)

        results, _ = await pipeline.search("test", scope="project_shared")

        ctx = results[0].context
        assert ctx is not None
        assert ctx.window_before == (chunks[0],)
        # The boundary still keeps user-tier neighbours visible.
        assert ctx.window_after == (chunks[2],)

    async def test_empty_scope_filter_keeps_the_boundary(self):
        """``scope=[]`` carries no intent — SQL answers it with ``scope='user'``.

        Reading the empty filter's permissive ``matches()`` as an opt-in would
        hand out other projects' chunks as context.
        """
        chunks = [
            _make_chunk(
                "other project",
                source="/tmp/s.md",
                start_line=0,
                scope="project_shared",
                project_root=Path("/other"),
            ),
            _make_chunk("anchor", source="/tmp/s.md", start_line=10),
        ]
        pipeline = _pipeline_for(chunks, chunks[1], window=1)

        results, _ = await pipeline.search("test", scope=[])

        ctx = results[0].context
        assert ctx is not None
        assert ctx.window_before == ()

    @pytest.mark.parametrize(
        ("kwargs", "neighbor_created"),
        [
            ({"tag_filter": "anchor-only"}, datetime(2025, 1, 1, tzinfo=UTC)),
            # Neighbours predate the lower bound; the anchor clears it.
            ({"created_from": datetime(2020, 1, 1, tzinfo=UTC)}, datetime(2010, 1, 1, tzinfo=UTC)),
            # Neighbours postdate the upper bound; the anchor clears it. Dating
            # them 2010 instead would satisfy the bound, and an implementation
            # that wrongly screened neighbours on it would still pass.
            (
                {"created_before": datetime(2030, 1, 1, tzinfo=UTC)},
                datetime(2035, 1, 1, tzinfo=UTC),
            ),
        ],
        ids=["tags", "created_from", "created_before"],
    )
    async def test_selection_filters_do_not_constrain_neighbors(self, kwargs, neighbor_created):
        """Selection filters say what was searched for, not what may be seen.

        Each case is built so only the anchor satisfies the filter — the
        neighbours carry no tags, or fall on the wrong side of the bound — so
        applying it to them would empty the window.
        """
        chunks = _make_file_chunks("/tmp/doc.md", 3)
        for i in (0, 2):
            chunks[i].created_at = neighbor_created
        chunks[1].created_at = datetime(2025, 1, 1, tzinfo=UTC)
        chunks[1].metadata = dataclasses.replace(chunks[1].metadata, tags=("anchor-only",))
        pipeline = _pipeline_for(chunks, chunks[1], window=1)

        results, _ = await pipeline.search("test", **kwargs)

        ctx = results[0].context
        assert ctx is not None
        assert ctx.window_before == (chunks[0],)
        assert ctx.window_after == (chunks[2],)

    async def test_anchor_hidden_by_a_concurrent_reindex_reports_no_raw_ordinal(self):
        """A re-index between retrieval and expansion keeps the id, not the row.

        The hit is still returned, so it has to be counted — but its position
        must come from the visible chunks ahead of it. The raw index would
        publish where it sits among the hidden ones.
        """
        chunks = _mixed_namespace_file()
        # The stored row for the anchor has moved into a hidden namespace
        # since the retrievers matched it; the SearchResult still holds the
        # copy that passed them.
        stale_anchor = chunks[2]
        chunks[2] = _make_chunk(
            stale_anchor.content,
            source="/tmp/mixed.md",
            start_line=stale_anchor.metadata.start_line,
            namespace="archive:2024",
            chunk_id=stale_anchor.id,
        )
        pipeline = _pipeline_for(chunks, stale_anchor, window=2)

        results, _ = await pipeline.search("test")

        ctx = results[0].context
        assert ctx is not None
        # Two visible chunks in the file (positions 0 and 4) plus the anchor.
        assert ctx.chunk_position == 2
        assert ctx.total_chunks_in_file == 3

    async def test_filter_only_path_applies_the_same_rule(self):
        """Empty query enumerates via recall_chunks but expands identically."""
        chunks = _mixed_namespace_file()
        pipeline = _make_pipeline(
            {Path("/tmp/mixed.md"): chunks},
            context_window_config=ContextWindowConfig(enabled=True, window_size=2),
        )
        pipeline._storage.recall_chunks = AsyncMock(return_value=[chunks[2]])

        results, _ = await pipeline.search("", tag_filter="anything")

        assert len(results) == 1
        ctx = results[0].context
        assert ctx is not None
        assert ctx.window_before == (chunks[0],)
        assert ctx.window_after == (chunks[4],)
