"""Search pipeline: BM25 + Dense + RRF fusion."""

from __future__ import annotations

import asyncio
import logging
from fnmatch import fnmatch
from typing import TYPE_CHECKING

from dataclasses import dataclass

from memtomem.config import AccessConfig, DecayConfig, MMRConfig, SearchConfig
from memtomem.models import NamespaceFilter, SearchResult
from memtomem.search.fusion import reciprocal_rank_fusion

logger = logging.getLogger(__name__)


def _match_source(filter_str: str, source_path: str) -> bool:
    """Match source_filter: glob when pattern chars present, substring otherwise."""
    if any(c in filter_str for c in ("*", "?", "[")):
        return fnmatch(source_path, filter_str)
    return filter_str in source_path


@dataclass
class RetrievalStats:
    bm25_candidates: int = 0
    dense_candidates: int = 0
    fused_total: int = 0
    final_total: int = 0
    bm25_error: str | None = None


if TYPE_CHECKING:
    from memtomem.embedding.base import EmbeddingProvider
    from memtomem.storage.base import StorageBackend


class SearchPipeline:
    def __init__(
        self,
        storage: StorageBackend,
        embedder: EmbeddingProvider,
        config: SearchConfig,
        decay_config: DecayConfig | None = None,
        mmr_config: MMRConfig | None = None,
        access_config: AccessConfig | None = None,
        reranker: object | None = None,
        expansion_config: object | None = None,
        importance_config: object | None = None,
    ) -> None:
        self._storage = storage
        self._embedder = embedder
        self._config = config
        self._decay_config = decay_config or DecayConfig()
        self._mmr_config = mmr_config or MMRConfig()
        self._access_config = access_config or AccessConfig()
        self._reranker = reranker
        self._expansion_config = expansion_config
        self._importance_config = importance_config

        # Search result TTL cache (per-instance)
        self._search_cache: dict[str, tuple[float, list[SearchResult], RetrievalStats]] = {}
        self._cache_ttl = config.cache_ttl

    def _cache_key(
        self,
        query: str,
        top_k: int,
        source_filter: str | None,
        tag_filter: str | None,
        namespace: str | list[str] | None,
    ) -> str:
        import hashlib

        raw = (
            f"{query}|{top_k}|{source_filter}|{tag_filter}|{namespace}"
            f"|bm25={self._config.enable_bm25}:{self._config.bm25_candidates}"
            f"|dense={self._config.enable_dense}:{self._config.dense_candidates}"
            f"|rrf_k={self._config.rrf_k}|w={tuple(self._config.rrf_weights)}"
            f"|decay={self._decay_config.enabled}:{self._decay_config.half_life_days}"
            f"|mmr={self._mmr_config.enabled}:{self._mmr_config.lambda_param}"
        )
        return hashlib.md5(raw.encode()).hexdigest()

    def invalidate_cache(self) -> None:
        """Clear the search result TTL cache (call after config changes)."""
        self._search_cache.clear()

    async def search(
        self,
        query: str,
        top_k: int | None = None,
        source_filter: str | None = None,
        tag_filter: str | None = None,
        namespace: str | list[str] | None = None,
        rrf_weights: list[float] | None = None,
    ) -> tuple[list[SearchResult], RetrievalStats]:
        top_k = top_k or self._config.default_top_k
        effective_weights = rrf_weights or self._config.rrf_weights

        # Check TTL cache for identical queries
        import time

        cache_key = self._cache_key(query, top_k, source_filter, tag_filter, namespace)
        if cache_key in self._search_cache:
            ts, cached_results, cached_stats = self._search_cache[cache_key]
            if time.time() - ts < self._cache_ttl:
                return cached_results, cached_stats
            del self._search_cache[cache_key]

        bm25_k = max(self._config.bm25_candidates, top_k)
        dense_k = max(self._config.dense_candidates, top_k)
        ns_filter = NamespaceFilter.parse(namespace)

        use_bm25 = self._config.enable_bm25
        use_dense = self._config.enable_dense

        # Stage 0: Query expansion
        if self._expansion_config and getattr(self._expansion_config, "enabled", False):
            from memtomem.search.expansion import expand_query_tags, expand_query_headings

            strategy = getattr(self._expansion_config, "strategy", "tags")
            max_terms = getattr(self._expansion_config, "max_terms", 3)
            if strategy in ("tags", "both"):
                query = await expand_query_tags(query, self._storage, max_terms)
            if strategy in ("headings", "both"):
                query = await expand_query_headings(query, self._storage, self._embedder, max_terms)

        # Stage 1 + 2: run enabled retrievers concurrently
        bm25_results: list[SearchResult] = []
        dense_results: list[SearchResult] = []
        query_embedding: list[float] = []
        bm25_error: str | None = None

        if use_bm25:
            bm25_task = asyncio.create_task(
                self._storage.bm25_search(query, top_k=bm25_k, namespace_filter=ns_filter)
            )
        if use_dense:
            try:
                query_embedding = await self._embedder.embed_query(query)
                dense_results = await self._storage.dense_search(
                    query_embedding, top_k=dense_k, namespace_filter=ns_filter
                )
            except Exception as exc:
                logger.warning("Dense search unavailable: %s", exc)
                dense_results = []
        if use_bm25:
            try:
                bm25_results = await bm25_task
            except Exception as exc:
                logger.warning("BM25 search failed: %s", exc)
                bm25_results = []
                bm25_error = str(exc)

        stats = RetrievalStats(
            bm25_candidates=len(bm25_results),
            dense_candidates=len(dense_results),
            bm25_error=bm25_error,
        )

        # Stage 3: fusion (or single-retriever passthrough)
        if use_bm25 and use_dense:
            fused = reciprocal_rank_fusion(
                [bm25_results, dense_results],
                k=self._config.rrf_k,
                top_k=top_k,
                weights=effective_weights,
            )
        elif use_bm25:
            fused = bm25_results[:top_k]
        elif use_dense:
            fused = dense_results[:top_k]
        else:
            fused = []
        stats.fused_total = len(fused)

        # Stage 3.5: Cross-encoder reranking
        if self._reranker is not None and fused:
            try:
                fused = await self._reranker.rerank(query, fused, top_k=top_k)
            except Exception as exc:
                logger.warning("Reranking failed, using original order: %s", exc)

        # Filter by source file if requested
        if source_filter:
            fused = [
                r for r in fused if _match_source(source_filter, str(r.chunk.metadata.source_file))
            ]

        # Filter by tag if requested (comma-separated = OR matching)
        if tag_filter:
            required = {t.strip() for t in tag_filter.split(",") if t.strip()}
            fused = [r for r in fused if required & set(r.chunk.metadata.tags)]

        # Stage 4: Time decay (re-score older chunks lower)
        if self._decay_config.enabled and fused:
            from memtomem.search.decay import apply_score_decay

            fused = apply_score_decay(fused, half_life_days=self._decay_config.half_life_days)

        # Stage 5: MMR diversity re-ranking
        if self._mmr_config.enabled and fused and use_dense:
            from memtomem.search.mmr import apply_mmr

            chunk_ids = [str(r.chunk.id) for r in fused]
            emb_dict_raw = await self._storage.get_embeddings_for_chunks(chunk_ids)
            if emb_dict_raw:
                from uuid import UUID

                emb_dict = {UUID(k): v for k, v in emb_dict_raw.items()}
                fused = apply_mmr(fused, emb_dict, lambda_param=self._mmr_config.lambda_param)

        # Stage 6: Access-frequency boost
        if self._access_config.enabled and fused:
            from memtomem.search.access import apply_access_boost

            chunk_ids = [r.chunk.id for r in fused]
            access_counts = await self._storage.get_access_counts(chunk_ids)
            fused = apply_access_boost(
                fused, access_counts, max_boost=self._access_config.max_boost
            )

        # Stage 7: Importance boost
        if self._importance_config and getattr(self._importance_config, "enabled", False) and fused:
            from memtomem.search.importance import apply_importance_boost

            chunk_ids_imp = [r.chunk.id for r in fused]
            imp_scores = await self._storage.get_importance_scores(chunk_ids_imp)
            fused = apply_importance_boost(
                fused,
                imp_scores,
                max_boost=getattr(self._importance_config, "max_boost", 1.5),
            )

        stats.final_total = len(fused)

        # Increment access counts for returned results (fire-and-forget)
        if fused:

            async def _increment():
                try:
                    await self._storage.increment_access([r.chunk.id for r in fused])
                except Exception:
                    logger.debug("Failed to increment access counts", exc_info=True)

            asyncio.create_task(_increment())

        # Save to query history (fire-and-forget)
        async def _save_history():
            try:
                emb = query_embedding if use_dense else []
                await self._storage.save_query_history(
                    query,
                    emb,
                    [str(r.chunk.id) for r in fused[:top_k]],
                    [r.score for r in fused[:top_k]],
                )
            except Exception:
                logger.debug("Failed to save query history", exc_info=True)

        asyncio.create_task(_save_history())

        # Store in TTL cache
        self._search_cache[cache_key] = (time.time(), fused, stats)
        # Evict old entries (keep max 50)
        if len(self._search_cache) > 50:
            oldest_key = min(self._search_cache, key=lambda k: self._search_cache[k][0])
            del self._search_cache[oldest_key]

        return fused, stats
