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
from memtomem.storage.base import SearchMetadataFilter
from memtomem.storage.sqlite_helpers import norm_path


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


def _apply_source_exact(hits: list[SearchResult], metadata_filter) -> list[SearchResult]:
    """Emulate the storage-level ``source_exact`` SQL filter (#2184).

    The rescue BM25 leg pushes its source restriction into storage, so a
    dispatch double serving both the organic and the rescue call must
    honor the filter or organic-only chunks would leak into the rescue
    leg. Both sides normalize through ``norm_path``, matching the real
    ``_metadata_filter_sql`` behavior.
    """
    if metadata_filter is None or not metadata_filter.source_exact:
        return hits
    allowed = {norm_path(Path(v)) for v in metadata_filter.source_exact}
    return [r for r in hits if norm_path(r.chunk.metadata.source_file) in allowed]


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
    s.get_chunks_shared_from_batch = AsyncMock(return_value={})
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
        storage.get_chunks_shared_from_batch.assert_not_called()

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
        storage.get_chunks_shared_from_batch = AsyncMock(
            return_value={
                summary_chunk.id: [
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
            }
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
        call_args = storage.get_chunks_shared_from_batch.await_args
        assert call_args.kwargs.get("link_type") == "summarizes"

    @pytest.mark.asyncio
    async def test_link_walk_is_one_round_trip_for_many_summaries(self):
        """#2184: N above-threshold summaries must cost exactly one
        ``chunk_links`` round trip, not N — the batch call receives every
        summary id at once."""
        cfg = SessionSummaryConfig(expansion_lookup_top_k=3, expansion_score_threshold=0.3)
        summaries = [_chunk(f"summary {i}", namespace="archive:session:abc") for i in range(3)]
        storage = _async_storage()
        storage.bm25_search = AsyncMock(
            return_value=[
                _sr(c, score=0.9, rank=i + 1, source="bm25") for i, c in enumerate(summaries)
            ]
        )
        pipeline = _make_pipeline(storage, session_summary_config=cfg)
        await pipeline._session_summary_boost_sources("q")

        assert storage.get_chunks_shared_from_batch.await_count == 1
        (batch_ids,) = storage.get_chunks_shared_from_batch.await_args.args
        assert set(batch_ids) == {c.id for c in summaries}

    @pytest.mark.asyncio
    async def test_batch_failure_degrades_all_summaries_to_organic(self):
        """#2184 error-semantics pin: the batched walk is all-or-nothing.
        A failure on the single statement mutes the rescue for every
        summary (degrade to organic) rather than isolating per summary —
        a SQLite failure would have failed each id identically anyway."""
        cfg = SessionSummaryConfig(expansion_lookup_top_k=3, expansion_score_threshold=0.3)
        summaries = [_chunk(f"summary {i}", namespace="archive:session:abc") for i in range(3)]
        storage = _async_storage()
        storage.bm25_search = AsyncMock(
            return_value=[
                _sr(c, score=0.9, rank=i + 1, source="bm25") for i, c in enumerate(summaries)
            ]
        )
        storage.get_chunks_shared_from_batch = AsyncMock(
            side_effect=RuntimeError("links table gone")
        )
        pipeline = _make_pipeline(storage, session_summary_config=cfg)
        assert await pipeline._session_summary_boost_sources("q") == set()
        storage.get_chunks_batch.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_links_yields_empty(self):
        cfg = SessionSummaryConfig(expansion_lookup_top_k=3, expansion_score_threshold=0.3)
        summary_chunk = _chunk("summary", namespace="archive:session:abc")
        storage = _async_storage()
        storage.bm25_search = AsyncMock(
            return_value=[_sr(summary_chunk, score=0.9, rank=1, source="bm25")]
        )
        storage.get_chunks_shared_from_batch = AsyncMock(return_value={})
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
            metadata_filter=None,
            **kwargs,
        ):
            # Archive lookup pattern
            if namespace_filter is not None and getattr(namespace_filter, "pattern", None) == (
                "archive:session:*"
            ):
                return [_sr(summary_chunk, score=0.9, rank=1, source="bm25")]
            # Organic pool — both chunks visible; the rescue call carries
            # a source_exact filter that strips the organic chunk.
            return _apply_source_exact(
                [
                    _sr(organic, score=1.0, rank=1, source="bm25"),
                    _sr(rescued, score=0.4, rank=2, source="bm25"),
                ],
                metadata_filter,
            )

        storage.bm25_search = AsyncMock(side_effect=bm25_dispatch)
        storage.get_chunks_shared_from_batch = AsyncMock(
            return_value={
                summary_chunk.id: [
                    ChunkLink(
                        target_id=rescued.id,
                        link_type="summarizes",
                        namespace_target="default",
                        created_at=datetime.now(timezone.utc),
                        source_id=summary_chunk.id,
                    )
                ]
            }
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
    async def test_zero_bm25_weight_gates_the_rescue_keyword_leg(self):
        """#2092: rescue's sub-retrievals inherit the caller's weight gate.

        With ``bm25_weight=0`` on a hybrid stack the rescue leg must not
        re-surface keyword-matched candidates through its own (positive)
        fusion weight — the rescue retrieval runs with ``use_bm25=False``.
        (With *every* leg gated off the rescue block is skipped
        entirely.)
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
            metadata_filter=None,
            **kwargs,
        ):
            if namespace_filter is not None and getattr(namespace_filter, "pattern", None) == (
                "archive:session:*"
            ):
                return [_sr(summary_chunk, score=0.9, rank=1, source="bm25")]
            return [_sr(rescued, score=0.4, rank=1, source="bm25")]

        storage.bm25_search = AsyncMock(side_effect=bm25_dispatch)
        storage.get_chunks_shared_from_batch = AsyncMock(
            return_value={
                summary_chunk.id: [
                    ChunkLink(
                        target_id=rescued.id,
                        link_type="summarizes",
                        namespace_target="default",
                        created_at=datetime.now(timezone.utc),
                        source_id=summary_chunk.id,
                    )
                ]
            }
        )
        storage.get_chunks_batch = AsyncMock(return_value={rescued.id: rescued})

        embedder = AsyncMock()
        embedder.embed_query = AsyncMock(return_value=[0.1] * 8)
        pipeline = SearchPipeline(
            storage=storage,
            embedder=embedder,
            config=SearchConfig(enable_bm25=True, enable_dense=True),
            session_summary_config=cfg,
        )
        rescue_kwargs: dict = {}
        real_rescue = pipeline._rescue_retrieval

        async def capture_rescue(*args, **kwargs):
            rescue_kwargs.update(kwargs)
            return await real_rescue(*args, **kwargs)

        pipeline._rescue_retrieval = capture_rescue

        results, _stats = await pipeline.search("q", top_k=10, rrf_weights=[0.0, 1.0])

        assert rescue_kwargs["use_bm25"] is False
        assert rescue_kwargs["use_dense"] is True
        assert rescued.id not in {r.chunk.id for r in results}

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
            metadata_filter=None,
            **kwargs,
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
        storage.get_chunks_shared_from_batch = AsyncMock(
            return_value={
                summary_chunk.id: [
                    ChunkLink(
                        target_id=rescued.id,
                        link_type="summarizes",
                        namespace_target="default",
                        created_at=datetime.now(timezone.utc),
                        source_id=summary_chunk.id,
                    )
                ]
            }
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
            metadata_filter=None,
            **kwargs,
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
            **kwargs,
        ):
            # ``**kwargs`` absorbs keyword-only additions to the real
            # ``dense_search`` signature (e.g. ``exhaustive`` from the replay
            # path, #1802) so this double stays call-compatible.
            dense_calls.append(
                {
                    "project_context_root": project_context_root,
                    "scope_filter": scope_filter,
                }
            )
            return [_sr(rescued, score=0.4, rank=1, source="dense")]

        storage.bm25_search = AsyncMock(side_effect=bm25_dispatch)
        storage.dense_search = AsyncMock(side_effect=dense_dispatch)
        storage.get_chunks_shared_from_batch = AsyncMock(
            return_value={
                summary_chunk.id: [
                    ChunkLink(
                        target_id=rescued.id,
                        link_type="summarizes",
                        namespace_target="default",
                        created_at=datetime.now(timezone.utc),
                        source_id=summary_chunk.id,
                    )
                ]
            }
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
            metadata_filter=None,
            **kwargs,
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
            query,
            top_k,
            namespace_filter=None,
            scope_filter=None,
            project_context_root=None,
            metadata_filter=None,
            **kwargs,
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
# 3a. Rescue BM25 leg pushes the source filter into storage SQL (#2184)
# ---------------------------------------------------------------------------


class TestRescueSourceFilterPushdown:
    """#2184: the rescue BM25 leg passes ``SearchMetadataFilter.source_exact``
    to storage instead of post-filtering in Python; the dense leg stays
    Python-filtered (see ``_rescue_retrieval`` docstring)."""

    @pytest.mark.asyncio
    async def test_rescue_leg_pushes_source_filter_into_storage(self):
        storage = _async_storage()
        captured: dict = {}

        async def bm25_dispatch(
            query,
            top_k,
            namespace_filter=None,
            scope_filter=None,
            project_context_root=None,
            metadata_filter=None,
            **kwargs,
        ):
            captured["metadata_filter"] = metadata_filter
            captured["namespace_filter"] = namespace_filter
            return []

        storage.bm25_search = AsyncMock(side_effect=bm25_dispatch)
        pipeline = _make_pipeline(storage, session_summary_config=SessionSummaryConfig())

        src = f"/{_CHUNK_SOURCE_BASE}/src/old_session.md"
        await pipeline._rescue_retrieval("q", [0.1] * 8, 10, {src}, use_bm25=True, use_dense=False)

        assert captured["namespace_filter"] is None
        # Expected value computed through the same normalizer the pipeline
        # uses — a literal POSIX string would fail on Windows.
        assert captured["metadata_filter"].source_exact == (norm_path(Path(src)),)

    @pytest.mark.asyncio
    async def test_outer_source_pin_intersects_after_normalization(self):
        """An outer ``source_exact`` pin composes by intersection, and both
        sides normalize first — a lexically different but canonically equal
        path (``src/../src/…``) must still intersect."""
        storage = _async_storage()
        captured: dict = {}

        async def bm25_dispatch(
            query,
            top_k,
            namespace_filter=None,
            scope_filter=None,
            project_context_root=None,
            metadata_filter=None,
            **kwargs,
        ):
            captured["metadata_filter"] = metadata_filter
            return []

        storage.bm25_search = AsyncMock(side_effect=bm25_dispatch)
        pipeline = _make_pipeline(storage, session_summary_config=SessionSummaryConfig())

        canonical = f"/{_CHUNK_SOURCE_BASE}/src/old_session.md"
        detoured = f"/{_CHUNK_SOURCE_BASE}/src/../src/old_session.md"
        outer = SearchMetadataFilter(source_exact=(detoured, f"/{_CHUNK_SOURCE_BASE}/other.md"))
        await pipeline._rescue_retrieval(
            "q",
            [0.1] * 8,
            10,
            {canonical},
            use_bm25=True,
            use_dense=False,
            metadata_filter=outer,
        )

        assert captured["metadata_filter"].source_exact == (norm_path(Path(canonical)),)

    @pytest.mark.asyncio
    async def test_dense_leg_does_not_receive_boost_source_filter(self):
        """M2 pin (Codex design review): the dense leg must NOT receive the
        boost-source filter — a selective ``source_exact`` set makes
        ``dense_search`` escalate its inner KNN K up to a full
        vector-table scan. Dense gets exactly the outer filter, or no
        ``metadata_filter`` kwarg at all when none was supplied."""
        storage = _async_storage()
        dense_captured: list[dict] = []

        async def dense_dispatch(
            embedding,
            top_k,
            namespace_filter=None,
            scope_filter=None,
            project_context_root=None,
            **kwargs,
        ):
            dense_captured.append(kwargs)
            return []

        storage.dense_search = AsyncMock(side_effect=dense_dispatch)
        pipeline = _make_pipeline(storage, session_summary_config=SessionSummaryConfig())
        src = f"/{_CHUNK_SOURCE_BASE}/src/old_session.md"

        # No outer filter → dense gets no metadata_filter kwarg at all.
        await pipeline._rescue_retrieval("q", [0.1] * 8, 10, {src}, use_bm25=True, use_dense=True)
        assert "metadata_filter" not in dense_captured[0]

        # Outer filter present → dense gets it verbatim, not the
        # boost-source-augmented bm25 filter.
        outer = SearchMetadataFilter(chunk_types=("code",))
        await pipeline._rescue_retrieval(
            "q",
            [0.1] * 8,
            10,
            {src},
            use_bm25=True,
            use_dense=True,
            metadata_filter=outer,
        )
        assert dense_captured[1]["metadata_filter"] is outer
        assert dense_captured[1]["metadata_filter"].source_exact == ()

    @pytest.mark.asyncio
    async def test_disjoint_outer_source_pin_skips_rescue_without_storage_call(self):
        storage = _async_storage()
        pipeline = _make_pipeline(storage, session_summary_config=SessionSummaryConfig())

        outer = SearchMetadataFilter(source_exact=(f"/{_CHUNK_SOURCE_BASE}/other.md",))
        result = await pipeline._rescue_retrieval(
            "q",
            [0.1] * 8,
            10,
            {f"/{_CHUNK_SOURCE_BASE}/src/old_session.md"},
            use_bm25=True,
            use_dense=True,
            metadata_filter=outer,
        )

        assert result == []
        storage.bm25_search.assert_not_called()
        storage.dense_search.assert_not_called()

    @pytest.mark.asyncio
    async def test_rescue_preserves_outer_time_and_type_bounds(self):
        """Outer ``chunk_types`` / ``created_from`` bounds must survive into
        the rescue-leg filter — the source pushdown replaces only
        ``source_exact``."""
        storage = _async_storage()
        captured: dict = {}

        async def bm25_dispatch(
            query,
            top_k,
            namespace_filter=None,
            scope_filter=None,
            project_context_root=None,
            metadata_filter=None,
            **kwargs,
        ):
            captured["metadata_filter"] = metadata_filter
            return []

        storage.bm25_search = AsyncMock(side_effect=bm25_dispatch)
        pipeline = _make_pipeline(storage, session_summary_config=SessionSummaryConfig())

        bound = datetime(2026, 1, 1, tzinfo=timezone.utc)
        outer = SearchMetadataFilter(chunk_types=("code",), created_from=bound)
        src = f"/{_CHUNK_SOURCE_BASE}/src/old_session.md"
        await pipeline._rescue_retrieval(
            "q",
            [0.1] * 8,
            10,
            {src},
            use_bm25=True,
            use_dense=False,
            metadata_filter=outer,
        )

        got = captured["metadata_filter"]
        assert got.chunk_types == ("code",)
        assert got.created_from == bound
        assert got.source_exact == (norm_path(Path(src)),)


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
            query,
            top_k,
            namespace_filter=None,
            scope_filter=None,
            project_context_root=None,
            metadata_filter=None,
            **kwargs,
        ):
            if getattr(namespace_filter, "pattern", None) == "archive:session:*":
                return [_sr(cfg_summary, score=0.9, rank=1, source="bm25")]
            return [_sr(organic, score=1.0, rank=1, source="bm25")]

        storage.bm25_search = AsyncMock(side_effect=bm25_dispatch)
        storage.get_chunks_shared_from_batch = AsyncMock(
            side_effect=RuntimeError("links table gone")
        )
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
            if r.levelno == logging.WARNING and "get_chunks_shared_from_batch failed" in r.message
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

        records = [r for r in caplog.records if "get_chunks_shared_from_batch failed" in r.message]
        assert [r.levelno for r in records] == [logging.WARNING, logging.DEBUG]

    @pytest.mark.asyncio
    async def test_summary_lookup_failure_warns_and_degrades(self, caplog):
        """The very first rescue stage (archive lookup) failing must also
        be loud and leave organic retrieval intact."""
        cfg = SessionSummaryConfig(expansion_lookup_top_k=3, expansion_score_threshold=0.3)
        organic = _chunk("organic chunk")
        storage = _async_storage()

        async def bm25_dispatch(
            query,
            top_k,
            namespace_filter=None,
            scope_filter=None,
            project_context_root=None,
            metadata_filter=None,
            **kwargs,
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
