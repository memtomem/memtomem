"""Tests for search pipeline stages (expansion, reranker, importance integration)."""

import asyncio

import pytest
from pathlib import Path
from uuid import uuid4
from memtomem.models import Chunk, ChunkMetadata, SearchResult


class TestMatchSourceFilter:
    """Pin: ``match_source_filter`` folds separators on both sides before
    comparing, so a POSIX-typed ``/tmp/keep/`` matches a Windows-stored
    ``\\tmp\\keep\\file.md`` (#720, sibling of #647).

    Without this pin a regression where someone removes the
    ``.replace("\\", "/")`` calls would still pass on POSIX (where
    backslashes never appear in native path strings) and only break on
    the Windows CI leg. Each row exercises a (filter shape, source
    shape) cross-product so both code branches (substring + glob) are
    covered with mixed separator inputs.
    """

    @pytest.mark.parametrize(
        "filter_str,source_path,expected",
        [
            # Substring branch — POSIX shape (identity on POSIX).
            ("/tmp/keep/", "/tmp/keep/policy.md", True),
            ("/tmp/other/", "/tmp/keep/policy.md", False),
            # Substring branch — POSIX filter, Windows-shape source.
            ("/tmp/keep/", "\\tmp\\keep\\policy.md", True),
            ("/tmp/other/", "\\tmp\\keep\\policy.md", False),
            # Substring branch — Windows filter, POSIX-shape source.
            ("\\tmp\\keep\\", "/tmp/keep/policy.md", True),
            # Substring branch — Windows-shape both sides.
            ("\\tmp\\keep\\", "\\tmp\\keep\\policy.md", True),
            # Glob branch — POSIX pattern, Windows-shape source.
            ("/tmp/*/policy.md", "\\tmp\\keep\\policy.md", True),
            ("/tmp/keep/*.txt", "\\tmp\\keep\\policy.md", False),
            # Glob branch — Windows pattern, POSIX-shape source.
            ("\\tmp\\*\\policy.md", "/tmp/keep/policy.md", True),
        ],
    )
    def test_separator_normalised_both_sides(self, filter_str, source_path, expected):
        from memtomem.search.pipeline import match_source_filter

        assert match_source_filter(filter_str, source_path) is expected


class TestMatchSourceFilterSubstring:
    """Pin: ``match_source_filter_substring`` folds separators on both
    sides before comparing — covers ``mem_decay`` /
    ``expire_chunks``, ``mem_auto_tag``, ``mem_export_chunks`` (#720).

    Substring-only contract: glob characters in the filter are treated
    as literals (no ``fnmatch`` fallback). The pin lets POSIX CI catch
    a future revert of the ``.replace("\\", "/")`` calls without
    depending on the Windows CI leg.
    """

    @pytest.mark.parametrize(
        "filter_str,source_path,expected",
        [
            # POSIX shape (identity on POSIX).
            ("/tmp/keep/", "/tmp/keep/policy.md", True),
            ("/tmp/other/", "/tmp/keep/policy.md", False),
            # POSIX filter, Windows-shape source — the #720 case.
            ("/tmp/keep/", "\\tmp\\keep\\policy.md", True),
            ("/tmp/other/", "\\tmp\\keep\\policy.md", False),
            # Windows filter, POSIX-shape source.
            ("\\tmp\\keep\\", "/tmp/keep/policy.md", True),
            # Windows-shape both sides.
            ("\\tmp\\keep\\", "\\tmp\\keep\\policy.md", True),
            # Glob characters in filter are literal — no fnmatch fallback.
            ("*.md", "\\tmp\\keep\\policy.md", False),
        ],
    )
    def test_separator_normalised_both_sides(self, filter_str, source_path, expected):
        from memtomem.search.pipeline import match_source_filter_substring

        assert match_source_filter_substring(filter_str, source_path) is expected


class TestMatchSourceFilterGlob:
    """Pin: ``match_source_filter_glob`` folds separators on both sides
    before ``fnmatch`` — covers ``mem_entity_scan`` (#720).

    Glob-only contract: substring filters that lack ``*?[`` characters
    only match exact filenames. The pin lets POSIX CI catch a future
    revert of the ``.replace("\\", "/")`` calls without depending on
    the Windows CI leg.
    """

    @pytest.mark.parametrize(
        "filter_str,source_path,expected",
        [
            # POSIX glob, Windows-shape source — the #720 case.
            ("/tmp/*/policy.md", "\\tmp\\keep\\policy.md", True),
            ("/tmp/keep/*.txt", "\\tmp\\keep\\policy.md", False),
            # Windows glob, POSIX-shape source.
            ("\\tmp\\*\\policy.md", "/tmp/keep/policy.md", True),
            # POSIX shape (identity on POSIX).
            ("/tmp/*/policy.md", "/tmp/keep/policy.md", True),
            # Substring-only filter — glob-only contract → no match.
            ("/tmp/keep", "\\tmp\\keep\\policy.md", False),
        ],
    )
    def test_separator_normalised_both_sides(self, filter_str, source_path, expected):
        from memtomem.search.pipeline import match_source_filter_glob

        assert match_source_filter_glob(filter_str, source_path) is expected


class TestPipelineQueryExpansion:
    """Test that query expansion modifies queries before retrieval."""

    @pytest.mark.asyncio
    async def test_tag_expansion_appends_terms(self):
        from memtomem.search.expansion import expand_query_tags

        class FakeStorage:
            async def get_tag_counts(self):
                return [("deployment", 10), ("kubernetes", 5), ("docker", 3)]

        result = await expand_query_tags("deploy containers", FakeStorage())
        assert "deployment" in result or result == "deploy containers"

    @pytest.mark.asyncio
    async def test_tag_expansion_no_match(self):
        from memtomem.search.expansion import expand_query_tags

        class FakeStorage:
            async def get_tag_counts(self):
                return [("python", 5)]

        result = await expand_query_tags("javascript frameworks", FakeStorage())
        assert result == "javascript frameworks"

    @pytest.mark.asyncio
    async def test_expansion_handles_error(self):
        from memtomem.search.expansion import expand_query_tags

        class BrokenStorage:
            async def get_tag_counts(self):
                raise RuntimeError("DB error")

        result = await expand_query_tags("test", BrokenStorage())
        assert result == "test"


class TestPipelineImportanceBoost:
    """Test importance boost re-ordering."""

    def _make_result(self, score, chunk_id=None):
        cid = chunk_id or uuid4()
        chunk = Chunk(
            content="test",
            metadata=ChunkMetadata(source_file=Path("/tmp/test.md")),
            id=cid,
            embedding=[],
        )
        return SearchResult(chunk=chunk, score=score, rank=1, source="test")

    def test_high_importance_reorders(self):
        from memtomem.search.importance import apply_importance_boost

        r1 = self._make_result(0.8)  # high score, no importance
        r2 = self._make_result(0.5)  # lower score, high importance
        scores = {str(r2.chunk.id): 1.0}

        boosted = apply_importance_boost([r1, r2], scores, max_boost=2.0)
        # r2 should be boosted: 0.5 * 2.0 = 1.0 > 0.8
        assert boosted[0].chunk.id == r2.chunk.id

    def test_zero_importance_no_change(self):
        from memtomem.search.importance import apply_importance_boost

        r1 = self._make_result(0.8)
        r2 = self._make_result(0.5)
        scores = {}  # no importance

        boosted = apply_importance_boost([r1, r2], scores)
        assert boosted[0].chunk.id == r1.chunk.id
        assert boosted[0].score == pytest.approx(0.8)


class TestBgTaskErrorCallback:
    """_bg_task_error_cb must log at warning when a fire-and-forget task raises."""

    @pytest.mark.asyncio
    async def test_callback_logs_warning_on_exception(self, caplog):
        import logging
        from memtomem.search.pipeline import _bg_task_error_cb

        async def _failing():
            raise RuntimeError("storage down")

        task = asyncio.create_task(_failing())
        task.add_done_callback(_bg_task_error_cb)

        with caplog.at_level(logging.WARNING, logger="memtomem.search.pipeline"):
            # Wait for the task to complete and the callback to fire.
            try:
                await task
            except RuntimeError:
                pass
            # The callback runs synchronously after the task finishes, but we
            # need a brief event-loop tick for it to execute.
            await asyncio.sleep(0)

        assert any("storage down" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_callback_silent_on_success(self, caplog):
        import logging
        from memtomem.search.pipeline import _bg_task_error_cb

        async def _ok():
            return 42

        task = asyncio.create_task(_ok())
        task.add_done_callback(_bg_task_error_cb)

        with caplog.at_level(logging.WARNING, logger="memtomem.search.pipeline"):
            await task
            await asyncio.sleep(0)

        assert not any("Background task" in r.message for r in caplog.records)


class TestImportanceCompute:
    """Test importance score edge cases."""

    def test_all_max(self):
        from memtomem.search.importance import compute_importance

        score = compute_importance(1000, 10, 50, 0.0)
        assert 0.8 <= score <= 1.0

    def test_all_zero_except_recency(self):
        from memtomem.search.importance import compute_importance

        score = compute_importance(0, 0, 0, 0.0)
        # recency factor = exp(0) = 1.0, weight = 0.2
        assert score == pytest.approx(0.2, abs=0.05)

    def test_very_old(self):
        from memtomem.search.importance import compute_importance

        score_new = compute_importance(10, 3, 2, 0.0)
        score_old = compute_importance(10, 3, 2, 1000.0)
        assert score_new > score_old


class TestRerankCandidatePool:
    """Regression for #307: RerankConfig.top_k must widen the rerank pool.

    Before the fix, ``SearchPipeline`` passed ``top_k`` (the response size) as
    the fusion cap, so the reranker could only reorder within the already-
    trimmed top-K and could not rescue relevant chunks RRF ranked just
    outside it.
    """

    @staticmethod
    def _make_result(
        content: str,
        rank: int,
        score: float | None = None,
        source_file: Path | None = None,
    ) -> SearchResult:
        chunk = Chunk(
            content=content,
            metadata=ChunkMetadata(source_file=source_file or Path(f"/tmp/{content}.md")),
            id=uuid4(),
            embedding=[],
        )
        return SearchResult(
            chunk=chunk,
            score=1.0 / rank if score is None else score,
            rank=rank,
            source="fused",
        )

    def _make_pipeline(
        self,
        bm25_results: list[SearchResult],
        *,
        reranker: object | None,
        rerank_config: object | None,
    ):
        from unittest.mock import AsyncMock

        from memtomem.config import SearchConfig
        from memtomem.search.pipeline import SearchPipeline

        storage = AsyncMock()
        storage.bm25_search = AsyncMock(return_value=bm25_results)
        storage.dense_search = AsyncMock(return_value=[])
        storage.increment_access = AsyncMock()
        storage.save_query_history = AsyncMock()
        storage.get_access_counts = AsyncMock(return_value={})
        storage.get_embeddings_for_chunks = AsyncMock(return_value={})
        storage.get_importance_scores = AsyncMock(return_value={})
        storage.count_chunks_by_ns_prefix = AsyncMock(return_value=0)

        embedder = AsyncMock()
        embedder.embed_query = AsyncMock(return_value=[0.1] * 8)

        return SearchPipeline(
            storage=storage,
            embedder=embedder,
            config=SearchConfig(enable_bm25=True, enable_dense=False),
            reranker=reranker,
            rerank_config=rerank_config,
        )

    @staticmethod
    def _probe_reranker(received: list[int], target_chunk_id=None):
        class _Probe:
            async def rerank(self, query, results, top_k):
                received.append(len(results))
                if target_chunk_id is None:
                    return results[:top_k]
                scored = [
                    SearchResult(
                        chunk=r.chunk,
                        score=1.0 if r.chunk.id == target_chunk_id else 0.01,
                        rank=r.rank,
                        source="reranked",
                    )
                    for r in results
                ]
                scored.sort(key=lambda r: r.score, reverse=True)
                return scored[:top_k]

        return _Probe()

    @pytest.mark.asyncio
    async def test_reranker_receives_widened_pool_rescuing_outranked_chunk(self):
        """RRF ranks the relevant chunk at position 15; the default
        ``oversample=2.0`` + ``min_pool=20`` must let the cross-encoder see
        it and surface it into the response."""
        from memtomem.config import RerankConfig

        fused_input = [self._make_result(f"chunk{i}", rank=i + 1) for i in range(20)]
        relevant = fused_input[14]

        received_pool_size: list[int] = []
        pipeline = self._make_pipeline(
            fused_input,
            reranker=self._probe_reranker(received_pool_size, relevant.chunk.id),
            rerank_config=RerankConfig(enabled=True),
        )

        results, _ = await pipeline.search("anything", top_k=10)

        # Default pool = max(20, min(200, 2.0*10)) = 20.
        assert received_pool_size == [20]
        assert results[0].chunk.id == relevant.chunk.id
        assert len(results) == 10

    @pytest.mark.asyncio
    async def test_pool_collapses_to_top_k_when_rerank_disabled(self):
        """No reranker + no rerank_config → single-retriever pool stays at top_k."""
        fused_input = [self._make_result(f"chunk{i}", rank=i + 1) for i in range(20)]

        pipeline = self._make_pipeline(fused_input, reranker=None, rerank_config=None)
        results, _ = await pipeline.search("anything", top_k=10)
        assert len(results) == 10

    @pytest.mark.asyncio
    async def test_excluded_roots_are_removed_before_top_k_truncation(self, tmp_path):
        pinned_root = tmp_path / "memories" / "pinned"

        def result(content: str, rank: int, source: Path) -> SearchResult:
            return SearchResult(
                chunk=Chunk(
                    content=content,
                    metadata=ChunkMetadata(source_file=source),
                    id=uuid4(),
                    embedding=[],
                ),
                score=1.0 / rank,
                rank=rank,
                source="bm25",
            )

        pinned = [
            result(f"pin{i}", i + 1, pinned_root / "agents" / "other" / f"{i}.md") for i in range(2)
        ]
        normal = [result(f"normal{i}", i + 3, tmp_path / "memories" / f"{i}.md") for i in range(2)]

        pipeline = self._make_pipeline([*pinned, *normal], reranker=None, rerank_config=None)
        results, _ = await pipeline.search("anything", top_k=2, exclude_source_roots=(pinned_root,))

        assert [result.chunk.content for result in results] == ["normal0", "normal1"]

    @pytest.mark.asyncio
    async def test_filter_only_search_excludes_roots(self, tmp_path):
        pinned_root = tmp_path / "memories" / "pinned"
        pinned = self._make_result(
            "pinned", rank=1, source_file=pinned_root / "general" / "policy.md"
        ).chunk
        normal = self._make_result(
            "normal", rank=2, source_file=tmp_path / "memories" / "normal.md"
        ).chunk

        pipeline = self._make_pipeline([], reranker=None, rerank_config=None)
        pipeline._storage.recall_chunks.return_value = [pinned, normal]

        results, _ = await pipeline.search(
            "",
            top_k=2,
            tag_filter="policy",
            exclude_source_roots=(pinned_root,),
        )

        assert [result.chunk.content for result in results] == ["normal"]

    @pytest.mark.asyncio
    async def test_excluded_roots_are_reapplied_after_context_expansion(self, tmp_path):
        from unittest.mock import AsyncMock

        pinned_root = tmp_path / "memories" / "pinned"
        normal = self._make_result(
            "normal", rank=1, source_file=tmp_path / "memories" / "normal.md"
        )
        pinned_neighbor = self._make_result(
            "pinned-neighbor",
            rank=2,
            source_file=pinned_root / "general" / "policy.md",
        )

        pipeline = self._make_pipeline([normal], reranker=None, rerank_config=None)
        pipeline._expand_context = AsyncMock(return_value=[normal, pinned_neighbor])

        results, _ = await pipeline.search(
            "anything",
            top_k=2,
            context_window=1,
            exclude_source_roots=(pinned_root,),
        )

        assert [result.chunk.content for result in results] == ["normal"]

    def test_exclusion_uses_path_boundaries_and_participates_in_cache_key(self, tmp_path):
        from memtomem.search.pipeline import _source_is_excluded

        root = (tmp_path / "pinned").resolve()
        assert _source_is_excluded(root / "general" / "policy.md", (root,)) is True
        assert _source_is_excluded(tmp_path / "pinned-other" / "policy.md", (root,)) is False

        pipeline = self._make_pipeline([], reranker=None, rerank_config=None)
        without = pipeline._cache_key("q", 10, None, None, None)
        with_root = pipeline._cache_key("q", 10, None, None, None, exclude_source_roots=(root,))
        assert without != with_root

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "top_k,oversample,min_pool,max_pool,expected_pool",
        [
            (10, 2.0, 20, 200, 20),  # default knobs at default top_k → 20
            (5, 2.0, 20, 200, 20),  # tiny request → floored at min_pool
            (20, 2.0, 20, 200, 40),  # scales with request (was stuck at 20 pre-fix)
            (50, 2.0, 20, 200, 100),  # scales with request
            (150, 2.0, 20, 200, 200),  # capped at max_pool
            (10, 1.5, 20, 200, 20),  # 1.5× * 10 = 15, floored to 20
            (40, 1.5, 20, 200, 60),  # 1.5× at mid-size
            (10, 2.0, 1, 200, 20),  # min_pool=1 → 2×10 wins
            (10, 2.0, 50, 200, 50),  # elevated floor wins
        ],
    )
    async def test_pool_size_scaling_table(
        self, top_k, oversample, min_pool, max_pool, expected_pool
    ):
        """Pool formula: ``max(min_pool, min(max_pool, int(oversample*top_k)))``."""
        from memtomem.config import RerankConfig

        fused_input = [self._make_result(f"chunk{i}", rank=i + 1) for i in range(250)]
        received: list[int] = []
        pipeline = self._make_pipeline(
            fused_input,
            reranker=self._probe_reranker(received),
            rerank_config=RerankConfig(
                enabled=True,
                oversample=oversample,
                min_pool=min_pool,
                max_pool=max_pool,
            ),
        )

        await pipeline.search("q", top_k=top_k)
        assert received == [expected_pool]

    def test_cache_key_changes_when_pool_knobs_change(self):
        """Enabling rerank or changing any of oversample/min_pool/max_pool
        must bust the cache."""
        from memtomem.config import RerankConfig

        class DummyReranker:
            async def rerank(self, query, results, top_k):
                return results[:top_k]

        base = self._make_pipeline([], reranker=None, rerank_config=None)
        key_off = base._cache_key("q", 10, None, None, None)

        def _key_for(**kwargs):
            pipe = self._make_pipeline(
                [],
                reranker=DummyReranker(),
                rerank_config=RerankConfig(enabled=True, **kwargs),
            )
            return pipe._cache_key("q", 10, None, None, None)

        key_default = _key_for()
        key_oversample = _key_for(oversample=3.0)
        key_min = _key_for(min_pool=30)
        key_max = _key_for(max_pool=100)

        assert len({key_off, key_default, key_oversample, key_min, key_max}) == 5

    def test_legacy_top_k_migrates_to_min_pool_with_deprecation(self):
        """``{rerank.top_k: 30}`` in legacy configs must warn and forward to min_pool."""
        import warnings

        from memtomem.config import RerankConfig

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            cfg = RerankConfig(enabled=True, top_k=30)

        assert cfg.min_pool == 30
        assert any(
            issubclass(w.category, DeprecationWarning) and "rerank.top_k" in str(w.message)
            for w in caught
        )

    def test_legacy_top_k_ignored_when_min_pool_also_set(self):
        import warnings

        from memtomem.config import RerankConfig

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            cfg = RerankConfig(enabled=True, top_k=99, min_pool=30)

        assert cfg.min_pool == 30
        assert any(
            issubclass(w.category, DeprecationWarning) and "ignored" in str(w.message)
            for w in caught
        )

    def test_max_pool_must_be_at_least_min_pool(self):
        from memtomem.config import RerankConfig

        with pytest.raises(ValueError, match="max_pool.*must be >= .*min_pool"):
            RerankConfig(enabled=True, min_pool=50, max_pool=10)

    def test_oversample_must_be_positive(self):
        from memtomem.config import RerankConfig

        with pytest.raises(ValueError, match="oversample"):
            RerankConfig(enabled=True, oversample=0.0)

    @pytest.mark.asyncio
    async def test_rerank_failure_falls_back_to_top_k_not_rerank_pool(self):
        """If the reranker raises, the caller must still get ``top_k`` items —
        not the wider ``rerank_pool`` that fusion produced upstream.

        Regression guard: PR #308 widened fusion to rerank_pool but the
        ``except`` branch left ``fused`` at that wider size, leaking pool
        size as response size.
        """
        from memtomem.config import RerankConfig

        fused_input = [self._make_result(f"chunk{i}", rank=i + 1) for i in range(20)]

        class BrokenReranker:
            async def rerank(self, query, results, top_k):
                raise RuntimeError("model unavailable")

        pipeline = self._make_pipeline(
            fused_input,
            reranker=BrokenReranker(),
            rerank_config=RerankConfig(enabled=True),
        )

        results, _ = await pipeline.search("anything", top_k=10)
        assert len(results) == 10

    def test_pool_knobs_registered_as_mutable(self):
        """Runtime mutation via `mm config set` / Web UI PATCH must accept
        oversample/min_pool/max_pool (provider/model still need restart)."""
        from memtomem.config import FIELD_CONSTRAINTS, MUTABLE_FIELDS

        assert MUTABLE_FIELDS["rerank"] == {"enabled", "oversample", "min_pool", "max_pool"}
        assert FIELD_CONSTRAINTS["rerank.oversample"]["type"] is float
        assert FIELD_CONSTRAINTS["rerank.min_pool"]["type"] is int
        assert FIELD_CONSTRAINTS["rerank.max_pool"]["type"] is int
        assert FIELD_CONSTRAINTS["rerank.enabled"]["type"] is bool


class TestFilterOnlySearch:
    """Empty-query path (#750): tag/source filter is the primary selector.

    Tag-pill click on a fresh session lands here — ``q`` is empty but
    ``tag_filter`` is set, and the user expects "show me all memos with
    this tag" rather than the pre-#750 no-op. The pipeline must skip
    BM25/dense/rerank (they need a query) and enumerate via
    ``recall_chunks`` so the filter can take over as the primary selector.
    """

    @staticmethod
    def _make_chunk(
        name: str,
        tags: tuple[str, ...] = (),
        valid_from_unix: int | None = None,
        valid_to_unix: int | None = None,
    ) -> Chunk:
        return Chunk(
            content=name,
            metadata=ChunkMetadata(
                source_file=Path(f"/tmp/{name}.md"),
                tags=tags,
                valid_from_unix=valid_from_unix,
                valid_to_unix=valid_to_unix,
            ),
            id=uuid4(),
            embedding=[],
        )

    def _make_pipeline(self, recall_return: list[Chunk]):
        from unittest.mock import AsyncMock

        from memtomem.config import SearchConfig
        from memtomem.search.pipeline import SearchPipeline

        storage = AsyncMock()
        storage.recall_chunks = AsyncMock(return_value=recall_return)
        # bm25/dense should never be reached on the empty-q path — set
        # them to raise so a regression that drops the early-return
        # branch fails loudly instead of silently returning [].
        storage.bm25_search = AsyncMock(side_effect=AssertionError("bm25 must not run"))
        storage.dense_search = AsyncMock(side_effect=AssertionError("dense must not run"))
        storage.increment_access = AsyncMock()
        storage.get_access_counts = AsyncMock(return_value={})
        storage.get_importance_scores = AsyncMock(return_value={})
        storage.count_chunks_by_ns_prefix = AsyncMock(return_value=0)

        embedder = AsyncMock()
        embedder.embed_query = AsyncMock(side_effect=AssertionError("embed_query must not run"))

        return storage, SearchPipeline(
            storage=storage,
            embedder=embedder,
            config=SearchConfig(enable_bm25=True, enable_dense=True),
        )

    @pytest.mark.asyncio
    async def test_empty_query_with_tag_filter_uses_recall(self):
        """``q="" + tag_filter`` enumerates via ``recall_chunks`` and
        forwards the tag through — not via BM25 with an empty query."""
        chunks = [
            self._make_chunk("a", tags=("redis",)),
            self._make_chunk("b", tags=("redis",)),
        ]
        storage, pipeline = self._make_pipeline(chunks)

        results, stats = await pipeline.search(query="", tag_filter="redis", top_k=5)

        storage.recall_chunks.assert_awaited_once()
        kwargs = storage.recall_chunks.await_args.kwargs
        assert kwargs["tag_filter"] == "redis"
        assert kwargs["source_filter"] is None
        assert len(results) == 2
        assert stats.fused_total == 2
        assert stats.final_total == 2
        assert all(r.source == "recall" for r in results)

    @pytest.mark.asyncio
    async def test_empty_query_with_source_filter_uses_recall(self):
        """Source-only path mirrors tag-only — same enumeration branch."""
        chunks = [self._make_chunk("notes")]
        storage, pipeline = self._make_pipeline(chunks)

        results, _ = await pipeline.search(query="  ", source_filter="notes.md", top_k=5)

        storage.recall_chunks.assert_awaited_once()
        kwargs = storage.recall_chunks.await_args.kwargs
        assert kwargs["source_filter"] == "notes.md"
        assert kwargs["tag_filter"] is None
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_empty_query_no_filters_returns_empty(self):
        """No keyword *and* no filter → return empty fast, do not enumerate."""
        storage, pipeline = self._make_pipeline([])

        results, stats = await pipeline.search(query="", top_k=5)

        storage.recall_chunks.assert_not_awaited()
        assert results == []
        assert stats.final_total == 0

    @pytest.mark.asyncio
    async def test_empty_query_trims_to_top_k(self):
        """Filter-only path honours ``top_k`` after the post-filter sort."""
        chunks = [self._make_chunk(f"c{i}", tags=("redis",)) for i in range(15)]
        _, pipeline = self._make_pipeline(chunks)

        results, _ = await pipeline.search(query="", tag_filter="redis", top_k=5)

        assert len(results) == 5

    @pytest.mark.asyncio
    async def test_validity_filter_drops_expired_on_filter_only(self):
        """Validity stage prunes expired chunks on the filter-only path
        too — without this guard a tag-only click on a fresh session
        could surface chunks whose ``valid_to`` has already passed,
        which the keyword path always filters out. Regression pin for
        the post-filter parity claim in the docstring."""
        live = self._make_chunk("live", tags=("redis",))
        expired = self._make_chunk(
            "expired",
            tags=("redis",),
            valid_to_unix=1_000_000_000,  # 2001 — long expired by any ``as_of``
        )
        _, pipeline = self._make_pipeline([live, expired])

        results, _ = await pipeline.search(query="", tag_filter="redis", top_k=10)

        ids = {r.chunk.id for r in results}
        assert live.id in ids
        assert expired.id not in ids

    @pytest.mark.asyncio
    async def test_decay_re_ranks_fresh_above_ancient(self):
        """The post-filter sort isn't just trim-then-truncate — decay
        actually re-scores so the freshest chunk lands first. Pins the
        "rank reflects recency × access × importance" contract: with
        decay enabled, a brand-new chunk must outrank a 10-year-old one
        even though both enter at score=1.0.
        """
        from datetime import datetime, timedelta, timezone
        from memtomem.config import DecayConfig

        from unittest.mock import AsyncMock
        from memtomem.config import SearchConfig
        from memtomem.search.pipeline import SearchPipeline

        now = datetime.now(timezone.utc)
        fresh = self._make_chunk("fresh", tags=("redis",))
        fresh.created_at = now
        fresh.updated_at = now
        ancient = self._make_chunk("ancient", tags=("redis",))
        ancient.created_at = now - timedelta(days=365 * 10)
        ancient.updated_at = now - timedelta(days=365 * 10)

        # Storage returns ancient first (mimicking ``ORDER BY created_at
        # DESC LIMIT`` returning a recency-ordered slice that happens to
        # not be in score order). The post-stages must re-rank.
        storage = AsyncMock()
        storage.recall_chunks = AsyncMock(return_value=[ancient, fresh])
        storage.bm25_search = AsyncMock(side_effect=AssertionError("bm25 must not run"))
        storage.dense_search = AsyncMock(side_effect=AssertionError("dense must not run"))
        storage.increment_access = AsyncMock()
        storage.get_access_counts = AsyncMock(return_value={})
        storage.get_importance_scores = AsyncMock(return_value={})
        storage.count_chunks_by_ns_prefix = AsyncMock(return_value=0)

        embedder = AsyncMock()
        embedder.embed_query = AsyncMock(side_effect=AssertionError("embed_query must not run"))

        pipeline = SearchPipeline(
            storage=storage,
            embedder=embedder,
            config=SearchConfig(enable_bm25=True, enable_dense=True),
            decay_config=DecayConfig(enabled=True, half_life_days=30.0),
        )

        results, _ = await pipeline.search(query="", tag_filter="redis", top_k=10)

        assert [r.chunk.id for r in results] == [fresh.id, ancient.id]
        assert results[0].score > results[1].score
