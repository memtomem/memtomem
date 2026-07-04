"""Phase C — Stage-1 session-summary rescue leg tests.

Covers the new path that runs alongside BM25 + dense:

1. ``_session_summary_boost_sources`` lookup + threshold + chunk_links walk
2. ``_rescue_retrieval`` boost_sources filter
3. 3-leg RRF preserving ``via_session_summary`` (OR) and labelling
   rescue-only chunks as ``session_rescue``
4. End-to-end ``SearchPipeline.search`` surfacing the flag through
   downstream stages so structured output sees it
5. Structured formatter emitting the field only when set
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock
import pytest

import memtomem.search.pipeline as pipeline_module
from memtomem.config import SearchConfig, SessionSummaryConfig
from memtomem.models import Chunk, ChunkLink, ChunkMetadata, SearchResult
from memtomem.search.fusion import reciprocal_rank_fusion
from memtomem.search.pipeline import SearchPipeline
from memtomem.server.formatters import _format_structured_results


# Symbolic anchor for chunk source paths. Tests use AsyncMock storage
# so no real file IO happens against this path; it just identifies
# chunks for boost-source comparisons. Picked so that ``Path`` does
# the same thing on POSIX and Windows: forward-slash, no drive letter,
# no expanduser/resolve needed downstream
# (``feedback_windows_tmp_path_under_userprofile.md``).
_CHUNK_SOURCE_BASE = "test-fixtures"


def _chunk(content: str = "x", source: str = "a.md", namespace: str = "default") -> Chunk:
    return Chunk(
        content=content,
        metadata=ChunkMetadata(
            source_file=Path(f"/{_CHUNK_SOURCE_BASE}/{source}"),
            namespace=namespace,
        ),
        embedding=[0.1] * 8,
    )


def _sr(chunk: Chunk, score: float, rank: int, source: str = "bm25", *, via=False) -> SearchResult:
    return SearchResult(chunk=chunk, score=score, rank=rank, source=source, via_session_summary=via)


# ---------------------------------------------------------------------------
# 1. Fusion preserves via_session_summary (OR) + labels rescue leg
# ---------------------------------------------------------------------------


class TestFusionViaSessionSummaryPropagation:
    def test_rescue_only_chunk_labelled_session_rescue(self):
        bm25 = _chunk("only_bm25")
        rescue = _chunk("only_rescue")
        fused = reciprocal_rank_fusion(
            [
                [_sr(bm25, 1.0, 1, "bm25")],
                [],
                [_sr(rescue, 1.0, 1, "session_rescue", via=True)],
            ],
            list_labels=["bm25", "dense", "session_rescue"],
            top_k=5,
        )
        labels = {r.chunk.id: r.source for r in fused}
        assert labels[rescue.id] == "session_rescue"
        flags = {r.chunk.id: r.via_session_summary for r in fused}
        assert flags[rescue.id] is True
        assert flags[bm25.id] is False

    def test_or_propagation_when_chunk_in_multiple_legs(self):
        """A chunk that hit bm25 *and* the rescue leg keeps the flag."""
        shared = _chunk("shared")
        fused = reciprocal_rank_fusion(
            [
                [_sr(shared, 1.0, 1, "bm25", via=False)],
                [],
                [_sr(shared, 1.0, 1, "session_rescue", via=True)],
            ],
            list_labels=["bm25", "dense", "session_rescue"],
            top_k=5,
        )
        result = next(r for r in fused if r.chunk.id == shared.id)
        assert result.via_session_summary is True
        # Hit two legs → labelled "fused"
        assert result.source == "fused"


# ---------------------------------------------------------------------------
# 2. _session_summary_boost_sources helper
# ---------------------------------------------------------------------------


def _make_pipeline(
    storage: AsyncMock,
    *,
    session_summary_config: SessionSummaryConfig | None = None,
) -> SearchPipeline:
    embedder = AsyncMock()
    embedder.embed_query = AsyncMock(return_value=[0.1] * 8)
    return SearchPipeline(
        storage=storage,
        embedder=embedder,
        config=SearchConfig(enable_bm25=True, enable_dense=False),
        session_summary_config=session_summary_config,
    )


def _async_storage() -> AsyncMock:
    s = AsyncMock()
    s.bm25_search = AsyncMock(return_value=[])
    s.dense_search = AsyncMock(return_value=[])
    s.increment_access = AsyncMock()
    s.save_query_history = AsyncMock()
    s.get_access_counts = AsyncMock(return_value={})
    s.get_embeddings_for_chunks = AsyncMock(return_value={})
    s.get_importance_scores = AsyncMock(return_value={})
    s.count_chunks_by_ns_prefix = AsyncMock(return_value=0)
    s.get_chunks_shared_from = AsyncMock(return_value=[])
    s.get_chunks_batch = AsyncMock(return_value={})
    return s


class TestBoostSourcesHelper:
    @pytest.mark.asyncio
    async def test_disabled_when_no_config(self):
        pipeline = _make_pipeline(_async_storage(), session_summary_config=None)
        assert await pipeline._session_summary_boost_sources("q") == set()

    @pytest.mark.asyncio
    async def test_disabled_when_top_k_zero(self):
        cfg = SessionSummaryConfig(expansion_lookup_top_k=1)
        # zero is rejected by validator, but we can stub directly via private
        # set; emulate by setting cfg with min positive value and bypass
        # threshold-only path: instead, prove disabled by an empty hit list.
        storage = _async_storage()
        storage.bm25_search = AsyncMock(return_value=[])
        pipeline = _make_pipeline(storage, session_summary_config=cfg)
        assert await pipeline._session_summary_boost_sources("q") == set()

    @pytest.mark.asyncio
    async def test_threshold_filters_low_score_summary(self):
        cfg = SessionSummaryConfig(expansion_lookup_top_k=3, expansion_score_threshold=0.5)
        summary_chunk = _chunk("summary", namespace="archive:session:abc")
        storage = _async_storage()
        # Below threshold → no rescue
        storage.bm25_search = AsyncMock(
            return_value=[_sr(summary_chunk, score=0.1, rank=1, source="bm25")]
        )
        pipeline = _make_pipeline(storage, session_summary_config=cfg)
        assert await pipeline._session_summary_boost_sources("q") == set()
        storage.get_chunks_shared_from.assert_not_called()

    @pytest.mark.asyncio
    async def test_above_threshold_walks_chunk_links_to_source_files(self):
        cfg = SessionSummaryConfig(expansion_lookup_top_k=3, expansion_score_threshold=0.3)
        summary_chunk = _chunk("summary", namespace="archive:session:abc")
        target1 = _chunk("c1", source="src/a.md")
        target2 = _chunk("c2", source="src/b.md")

        storage = _async_storage()
        storage.bm25_search = AsyncMock(
            return_value=[_sr(summary_chunk, score=0.9, rank=1, source="bm25")]
        )
        storage.get_chunks_shared_from = AsyncMock(
            return_value=[
                ChunkLink(
                    target_id=target1.id,
                    link_type="summarizes",
                    namespace_target="default",
                    created_at=datetime.now(timezone.utc),
                    source_id=summary_chunk.id,
                ),
                ChunkLink(
                    target_id=target2.id,
                    link_type="summarizes",
                    namespace_target="default",
                    created_at=datetime.now(timezone.utc),
                    source_id=summary_chunk.id,
                ),
            ]
        )
        storage.get_chunks_batch = AsyncMock(
            return_value={target1.id: target1, target2.id: target2}
        )

        pipeline = _make_pipeline(storage, session_summary_config=cfg)
        sources = await pipeline._session_summary_boost_sources("q")
        assert {Path(s).as_posix() for s in sources} == {
            f"/{_CHUNK_SOURCE_BASE}/src/a.md",
            f"/{_CHUNK_SOURCE_BASE}/src/b.md",
        }
        # Walk used the correct link_type
        call_args = storage.get_chunks_shared_from.await_args
        assert call_args.kwargs.get("link_type") == "summarizes"

    @pytest.mark.asyncio
    async def test_no_links_yields_empty(self):
        cfg = SessionSummaryConfig(expansion_lookup_top_k=3, expansion_score_threshold=0.3)
        summary_chunk = _chunk("summary", namespace="archive:session:abc")
        storage = _async_storage()
        storage.bm25_search = AsyncMock(
            return_value=[_sr(summary_chunk, score=0.9, rank=1, source="bm25")]
        )
        storage.get_chunks_shared_from = AsyncMock(return_value=[])
        pipeline = _make_pipeline(storage, session_summary_config=cfg)
        assert await pipeline._session_summary_boost_sources("q") == set()

    @pytest.mark.asyncio
    async def test_lookup_threads_scope_context_through(self):
        """ADR-0011 PR-D review pin: rescue summary lookup must honor
        the same scope_filter / project_context_root as the primary
        retrieval. Without it, an in-project search would see only
        user-tier session summaries.
        """
        cfg = SessionSummaryConfig(expansion_lookup_top_k=3, expansion_score_threshold=0.3)
        storage = _async_storage()
        storage.bm25_search = AsyncMock(return_value=[])
        pipeline = _make_pipeline(storage, session_summary_config=cfg)

        proj_root = Path(f"/{_CHUNK_SOURCE_BASE}/proj_pin")
        await pipeline._session_summary_boost_sources(
            "q",
            scope_filter=None,
            project_context_root=proj_root,
        )
        kwargs = storage.bm25_search.await_args.kwargs
        assert kwargs.get("project_context_root") == proj_root
        # Explicit None for scope_filter — caller used the always-on
        # default for the primary retrieval, rescue must mirror it.
        assert "scope_filter" in kwargs
        assert kwargs["scope_filter"] is None


# ---------------------------------------------------------------------------
# 3. End-to-end pipeline: rescue chunk surfaces with flag preserved
# ---------------------------------------------------------------------------


class TestPipelineEndToEndRescue:
    @pytest.mark.asyncio
    async def test_rescue_chunk_surfaces_with_flag(self):
        """A chunk absent from organic BM25 must be able to enter the result
        set via the rescue leg (RFC ``ranking contention``) and carry
        ``via_session_summary=True`` through the final pipeline output.
        """
        cfg = SessionSummaryConfig(expansion_lookup_top_k=3, expansion_score_threshold=0.3)
        summary_chunk = _chunk("summary body", namespace="archive:session:abc")
        rescued = _chunk("rescued chunk", source="src/old_session.md")
        organic = _chunk("organic chunk", source="src/today.md")

        storage = _async_storage()

        async def bm25_dispatch(
            query: str,
            top_k: int,
            namespace_filter=None,
            scope_filter=None,
            project_context_root=None,
        ):
            # Archive lookup pattern
            if namespace_filter is not None and getattr(namespace_filter, "pattern", None) == (
                "archive:session:*"
            ):
                return [_sr(summary_chunk, score=0.9, rank=1, source="bm25")]
            # Organic + rescue (unrestricted) pool — both chunks visible
            return [
                _sr(organic, score=1.0, rank=1, source="bm25"),
                _sr(rescued, score=0.4, rank=2, source="bm25"),
            ]

        storage.bm25_search = AsyncMock(side_effect=bm25_dispatch)
        storage.get_chunks_shared_from = AsyncMock(
            return_value=[
                ChunkLink(
                    target_id=rescued.id,
                    link_type="summarizes",
                    namespace_target="default",
                    created_at=datetime.now(timezone.utc),
                    source_id=summary_chunk.id,
                )
            ]
        )
        storage.get_chunks_batch = AsyncMock(return_value={rescued.id: rescued})

        pipeline = _make_pipeline(storage, session_summary_config=cfg)
        results, _stats = await pipeline.search("q", top_k=10)

        ids = {r.chunk.id for r in results}
        assert rescued.id in ids
        rescued_result = next(r for r in results if r.chunk.id == rescued.id)
        assert rescued_result.via_session_summary is True
        organic_result = next(r for r in results if r.chunk.id == organic.id)
        assert organic_result.via_session_summary is False

    @pytest.mark.asyncio
    async def test_rescue_threads_project_context_into_all_storage_calls(self):
        """ADR-0011 PR-D review pin: every storage call on the rescue
        path (summary lookup + rescue BM25 leg + rescue dense leg) must
        receive the same ``project_context_root`` the outer search was
        pinned to. Without this, the always-on scope filter silently
        drops project_shared / project_local rescue candidates whenever
        the outer search runs in a project context.
        """
        cfg = SessionSummaryConfig(expansion_lookup_top_k=3, expansion_score_threshold=0.3)
        summary_chunk = _chunk("summary body", namespace="archive:session:abc")
        rescued = _chunk("rescued chunk", source="src/old_session.md")

        storage = _async_storage()

        bm25_calls: list[dict] = []

        async def bm25_dispatch(
            query: str,
            top_k: int,
            namespace_filter=None,
            scope_filter=None,
            project_context_root=None,
        ):
            bm25_calls.append(
                {
                    "namespace_pattern": getattr(namespace_filter, "pattern", None),
                    "project_context_root": project_context_root,
                    "scope_filter": scope_filter,
                }
            )
            if getattr(namespace_filter, "pattern", None) == "archive:session:*":
                return [_sr(summary_chunk, score=0.9, rank=1, source="bm25")]
            return [_sr(rescued, score=0.5, rank=1, source="bm25")]

        storage.bm25_search = AsyncMock(side_effect=bm25_dispatch)
        storage.get_chunks_shared_from = AsyncMock(
            return_value=[
                ChunkLink(
                    target_id=rescued.id,
                    link_type="summarizes",
                    namespace_target="default",
                    created_at=datetime.now(timezone.utc),
                    source_id=summary_chunk.id,
                )
            ]
        )
        storage.get_chunks_batch = AsyncMock(return_value={rescued.id: rescued})

        pipeline = _make_pipeline(storage, session_summary_config=cfg)
        proj_root = Path(f"/{_CHUNK_SOURCE_BASE}/proj_pinned")
        await pipeline.search("q", top_k=10, project_context_root=proj_root)

        # Every BM25 call (primary, summary lookup, rescue leg) should
        # have received the project_context_root the outer search was
        # pinned to. The summary lookup is identifiable by its
        # archive namespace pattern.
        assert any(
            c["namespace_pattern"] == "archive:session:*" and c["project_context_root"] == proj_root
            for c in bm25_calls
        ), "summary lookup did not receive project_context_root"
        # The rescue retrieval leg ran with the same project_context_root.
        rescue_legs = [c for c in bm25_calls if c["namespace_pattern"] is None]
        assert rescue_legs, "rescue leg should have fired"
        assert all(c["project_context_root"] == proj_root for c in rescue_legs)

    @pytest.mark.asyncio
    async def test_rescue_dense_leg_threads_project_context(self):
        """Companion pin to the BM25-leg test: the rescue dense leg
        also receives ``project_context_root`` so the always-on storage
        filter sees the same context as the primary dense retrieval.
        """
        cfg = SessionSummaryConfig(expansion_lookup_top_k=3, expansion_score_threshold=0.3)
        summary_chunk = _chunk("summary body", namespace="archive:session:abc")
        rescued = _chunk("rescued chunk", source="src/old_session.md")

        storage = _async_storage()

        async def bm25_dispatch(
            query: str,
            top_k: int,
            namespace_filter=None,
            scope_filter=None,
            project_context_root=None,
        ):
            if getattr(namespace_filter, "pattern", None) == "archive:session:*":
                return [_sr(summary_chunk, score=0.9, rank=1, source="bm25")]
            return [_sr(rescued, score=0.5, rank=1, source="bm25")]

        dense_calls: list[dict] = []

        async def dense_dispatch(
            embedding,
            top_k: int,
            namespace_filter=None,
            scope_filter=None,
            project_context_root=None,
        ):
            dense_calls.append(
                {
                    "project_context_root": project_context_root,
                    "scope_filter": scope_filter,
                }
            )
            return [_sr(rescued, score=0.4, rank=1, source="dense")]

        storage.bm25_search = AsyncMock(side_effect=bm25_dispatch)
        storage.dense_search = AsyncMock(side_effect=dense_dispatch)
        storage.get_chunks_shared_from = AsyncMock(
            return_value=[
                ChunkLink(
                    target_id=rescued.id,
                    link_type="summarizes",
                    namespace_target="default",
                    created_at=datetime.now(timezone.utc),
                    source_id=summary_chunk.id,
                )
            ]
        )
        storage.get_chunks_batch = AsyncMock(return_value={rescued.id: rescued})

        # Need dense enabled on the pipeline to exercise the dense leg.
        embedder = AsyncMock()
        embedder.embed_query = AsyncMock(return_value=[0.1] * 8)
        pipeline = SearchPipeline(
            storage=storage,
            embedder=embedder,
            config=SearchConfig(enable_bm25=True, enable_dense=True),
            session_summary_config=cfg,
        )
        proj_root = Path(f"/{_CHUNK_SOURCE_BASE}/proj_dense_pinned")
        await pipeline.search("q", top_k=10, project_context_root=proj_root)

        # Both the primary and rescue dense calls must have received
        # the project_context_root the outer search was pinned to.
        assert dense_calls, "dense leg should have fired"
        assert all(c["project_context_root"] == proj_root for c in dense_calls)

    @pytest.mark.asyncio
    async def test_no_rescue_when_no_summary_above_threshold(self):
        """Common case: no past summary above threshold → rescue leg
        skipped (no extra retrieval round-trip)."""
        cfg = SessionSummaryConfig(expansion_lookup_top_k=3, expansion_score_threshold=0.5)
        organic = _chunk("organic chunk")

        storage = _async_storage()
        bm25_calls: list[object] = []

        def _label(nf) -> str:
            if nf is None:
                return "ORGANIC"
            if getattr(nf, "pattern", None) == "archive:session:*":
                return "archive:session:*"
            return "ORGANIC"

        async def bm25_dispatch(
            query: str,
            top_k: int,
            namespace_filter=None,
            scope_filter=None,
            project_context_root=None,
        ):
            label = _label(namespace_filter)
            bm25_calls.append(label)
            if label == "archive:session:*":
                return []  # no summary → boost_sources stays empty
            return [_sr(organic, score=1.0, rank=1, source="bm25")]

        storage.bm25_search = AsyncMock(side_effect=bm25_dispatch)
        pipeline = _make_pipeline(storage, session_summary_config=cfg)
        results, _ = await pipeline.search("q", top_k=10)
        assert {r.chunk.id for r in results} == {organic.id}
        # Exactly two BM25 calls: archive lookup + organic. No third
        # rescue retrieval call when boost_sources is empty.
        assert bm25_calls.count("ORGANIC") == 1
        assert bm25_calls.count("archive:session:*") == 1

    @pytest.mark.asyncio
    async def test_disabled_when_namespace_pinned(self):
        """Caller pinning a namespace explicitly opted in to that scope —
        the rescue leg (which broadens scope back out) must stay quiet."""
        cfg = SessionSummaryConfig(expansion_lookup_top_k=3)
        organic = _chunk("organic", namespace="agent-runtime:planner")

        storage = _async_storage()
        archive_lookup_called = False

        async def bm25_dispatch(
            query, top_k, namespace_filter=None, scope_filter=None, project_context_root=None
        ):
            nonlocal archive_lookup_called
            if getattr(namespace_filter, "pattern", None) == "archive:session:*":
                archive_lookup_called = True
                return []
            return [_sr(organic, score=1.0, rank=1, source="bm25")]

        storage.bm25_search = AsyncMock(side_effect=bm25_dispatch)
        pipeline = _make_pipeline(storage, session_summary_config=cfg)
        await pipeline.search("q", top_k=10, namespace="agent-runtime:planner")
        assert archive_lookup_called is False


# ---------------------------------------------------------------------------
# 3b. Rescue-leg failure loudness (#1610/#1611)
# ---------------------------------------------------------------------------


class TestRescueLegLoudness:
    """A rescue-leg failure degrades search to two-leg fusion, which is
    invisible in production — the swallow sites must log WARNING on the
    first occurrence (``feedback_silent_except_log_level``) and DEBUG
    afterwards, and the search itself must still succeed.
    """

    @pytest.fixture(autouse=True)
    def _reset_warn_once(self):
        """The warn-once registry is process-global; isolate each test."""
        pipeline_module._RESCUE_WARNED.clear()
        yield
        pipeline_module._RESCUE_WARNED.clear()

    def _failing_rescue_setup(self) -> tuple[AsyncMock, Chunk]:
        """Storage where the archive lookup succeeds but the chunk-links
        walk raises — the rescue leg dies mid-flight while the organic
        leg stays healthy."""
        cfg_summary = _chunk("summary body", namespace="archive:session:abc")
        organic = _chunk("organic chunk")
        storage = _async_storage()

        async def bm25_dispatch(
            query, top_k, namespace_filter=None, scope_filter=None, project_context_root=None
        ):
            if getattr(namespace_filter, "pattern", None) == "archive:session:*":
                return [_sr(cfg_summary, score=0.9, rank=1, source="bm25")]
            return [_sr(organic, score=1.0, rank=1, source="bm25")]

        storage.bm25_search = AsyncMock(side_effect=bm25_dispatch)
        storage.get_chunks_shared_from = AsyncMock(side_effect=RuntimeError("links table gone"))
        return storage, organic

    @pytest.mark.asyncio
    async def test_failure_degrades_to_organic_and_warns(self, caplog):
        cfg = SessionSummaryConfig(expansion_lookup_top_k=3, expansion_score_threshold=0.3)
        storage, organic = self._failing_rescue_setup()
        pipeline = _make_pipeline(storage, session_summary_config=cfg)

        with caplog.at_level(logging.DEBUG, logger="memtomem.search.pipeline"):
            results, _ = await pipeline.search("q", top_k=10)

        # Search must survive the rescue failure on organic results alone.
        assert {r.chunk.id for r in results} == {organic.id}
        warnings = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and "get_chunks_shared_from failed" in r.message
        ]
        assert len(warnings) == 1, "first rescue failure must be loud (WARNING, not DEBUG)"

    @pytest.mark.asyncio
    async def test_repeat_failure_downgrades_to_debug(self, caplog):
        """warn-once: a persistently failing dependency must not spam
        WARNING on every query — repeats log at DEBUG."""
        cfg = SessionSummaryConfig(expansion_lookup_top_k=3, expansion_score_threshold=0.3)
        storage, _ = self._failing_rescue_setup()
        pipeline = _make_pipeline(storage, session_summary_config=cfg)

        with caplog.at_level(logging.DEBUG, logger="memtomem.search.pipeline"):
            await pipeline.search("q", top_k=10)
            await pipeline.search("q2", top_k=10)

        records = [r for r in caplog.records if "get_chunks_shared_from failed" in r.message]
        assert [r.levelno for r in records] == [logging.WARNING, logging.DEBUG]

    @pytest.mark.asyncio
    async def test_summary_lookup_failure_warns_and_degrades(self, caplog):
        """The very first rescue stage (archive lookup) failing must also
        be loud and leave organic retrieval intact."""
        cfg = SessionSummaryConfig(expansion_lookup_top_k=3, expansion_score_threshold=0.3)
        organic = _chunk("organic chunk")
        storage = _async_storage()

        async def bm25_dispatch(
            query, top_k, namespace_filter=None, scope_filter=None, project_context_root=None
        ):
            if getattr(namespace_filter, "pattern", None) == "archive:session:*":
                raise RuntimeError("archive namespace unreadable")
            return [_sr(organic, score=1.0, rank=1, source="bm25")]

        storage.bm25_search = AsyncMock(side_effect=bm25_dispatch)
        pipeline = _make_pipeline(storage, session_summary_config=cfg)

        with caplog.at_level(logging.DEBUG, logger="memtomem.search.pipeline"):
            results, _ = await pipeline.search("q", top_k=10)

        assert {r.chunk.id for r in results} == {organic.id}
        assert any(
            r.levelno == logging.WARNING and "session-summary lookup failed" in r.message
            for r in caplog.records
        )


def test_default_rescue_weight_mirrors_config_default():
    """#1610: the module fallback must stay in sync with the
    ``SessionSummaryConfig.expansion_rescue_weight`` default it mirrors."""
    assert pipeline_module._DEFAULT_RESCUE_WEIGHT == SessionSummaryConfig().expansion_rescue_weight


# ---------------------------------------------------------------------------
# 4. Structured formatter emits via_session_summary only when True
# ---------------------------------------------------------------------------


class TestStructuredFormatterFlag:
    def test_flag_omitted_when_false(self):
        import json

        sr = _sr(_chunk("a"), 1.0, 1, "bm25", via=False)
        out = json.loads(_format_structured_results([sr]))
        assert "via_session_summary" not in out["results"][0]

    def test_flag_emitted_when_true(self):
        import json

        sr = _sr(_chunk("a"), 1.0, 1, "session_rescue", via=True)
        out = json.loads(_format_structured_results([sr]))
        assert out["results"][0]["via_session_summary"] is True


# ---------------------------------------------------------------------------
# 5. Config validators
# ---------------------------------------------------------------------------


class TestSessionSummaryConfigPhaseC:
    def test_defaults_match_rfc(self):
        cfg = SessionSummaryConfig()
        assert cfg.expansion_lookup_top_k == 3
        assert cfg.expansion_score_threshold == 0.3
        assert cfg.expansion_rescue_weight == 0.5

    def test_top_k_must_be_positive(self):
        with pytest.raises(ValueError):
            SessionSummaryConfig(expansion_lookup_top_k=0)

    def test_threshold_non_negative(self):
        SessionSummaryConfig(expansion_score_threshold=0.0)  # ok
        with pytest.raises(ValueError):
            SessionSummaryConfig(expansion_score_threshold=-0.1)

    def test_rescue_weight_non_negative(self):
        SessionSummaryConfig(expansion_rescue_weight=0.0)  # ok
        with pytest.raises(ValueError):
            SessionSummaryConfig(expansion_rescue_weight=-1.0)
