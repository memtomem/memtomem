"""Search pipeline: BM25 + Dense + RRF fusion."""

from __future__ import annotations

import asyncio
import logging
from fnmatch import fnmatch
from typing import TYPE_CHECKING

from dataclasses import dataclass

from memtomem.config import (
    MAX_CONTEXT_WINDOW_CHUNKS,
    AccessConfig,
    ContextWindowConfig,
    DecayConfig,
    MMRConfig,
    RerankConfig,
    SearchConfig,
)
from memtomem.models import ContextInfo, NamespaceFilter, SearchResult
from memtomem.search.fusion import reciprocal_rank_fusion

logger = logging.getLogger(__name__)


def _bg_task_error_cb(task: asyncio.Task) -> None:
    """Log errors from fire-and-forget background tasks."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        logger.warning("Background task %s failed: %s", task.get_name(), exc)


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
    dense_error: str | None = None
    # Chunks that live in namespaces matching ``system_namespace_prefixes``
    # (e.g. ``archive:*``) and were therefore excluded from the default,
    # namespace=None search. Non-zero only when the caller did not pick an
    # explicit namespace — surfaces as a hint in mem_search's output so
    # users know their archived memories still exist.
    hidden_system_ns: int = 0


if TYPE_CHECKING:
    from memtomem.embedding.base import EmbeddingProvider
    from memtomem.llm.base import LLMProvider
    from memtomem.storage.base import StorageBackend


_EXPANSION_CACHE_MAX = 100


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
        rerank_config: RerankConfig | None = None,
        expansion_config: object | None = None,
        importance_config: object | None = None,
        context_window_config: ContextWindowConfig | None = None,
        llm_provider: LLMProvider | None = None,
    ) -> None:
        self._storage = storage
        self._embedder = embedder
        self._config = config
        self._decay_config = decay_config or DecayConfig()
        self._mmr_config = mmr_config or MMRConfig()
        self._access_config = access_config or AccessConfig()
        self._reranker = reranker
        self._rerank_config = rerank_config
        self._expansion_config = expansion_config
        self._importance_config = importance_config
        self._context_window_config = context_window_config
        self._llm_provider = llm_provider

        # Search result TTL cache (per-instance) with version counter
        self._search_cache: dict[str, tuple[float, int, list[SearchResult], RetrievalStats]] = {}
        self._cache_ttl = config.cache_ttl
        self._cache_version = 0
        self._bg_tasks: set[asyncio.Task] = set()

        # LLM query expansion cache (cleared on invalidate_cache)
        self._expansion_cache: dict[str, str] = {}

    def _cache_key(
        self,
        query: str,
        top_k: int,
        source_filter: str | None,
        tag_filter: str | None,
        namespace: str | list[str] | None,
        context_window: int | None = None,
    ) -> str:
        import hashlib

        ctx_win = self._resolve_context_window(context_window)
        if self._reranker is not None and self._rerank_config is not None:
            rcfg = self._rerank_config
            rerank_signal = f"on:{rcfg.oversample}:{rcfg.min_pool}:{rcfg.max_pool}"
        else:
            rerank_signal = "off"
        raw = (
            f"{query}|{top_k}|{source_filter}|{tag_filter}|{namespace}"
            f"|bm25={self._config.enable_bm25}:{self._config.bm25_candidates}"
            f"|dense={self._config.enable_dense}:{self._config.dense_candidates}"
            f"|rrf_k={self._config.rrf_k}|w={tuple(self._config.rrf_weights)}"
            f"|decay={self._decay_config.enabled}:{self._decay_config.half_life_days}"
            f"|mmr={self._mmr_config.enabled}:{self._mmr_config.lambda_param}"
            f"|ctx_win={ctx_win}"
            f"|rerank={rerank_signal}"
        )
        return hashlib.md5(raw.encode()).hexdigest()

    def invalidate_cache(self) -> None:
        """Clear the search result TTL cache (call after data/config changes)."""
        self._cache_version += 1
        self._search_cache.clear()
        self._expansion_cache.clear()

    def _resolve_context_window(self, override: int | None) -> int:
        """Return the effective context window size (0 = disabled)."""
        if override is not None:
            return max(0, min(override, MAX_CONTEXT_WINDOW_CHUNKS))
        cfg = self._context_window_config
        if cfg and cfg.enabled:
            return max(0, min(cfg.window_size, MAX_CONTEXT_WINDOW_CHUNKS))
        return 0

    async def _expand_context(self, results: list[SearchResult], window: int) -> list[SearchResult]:
        """Attach ±window adjacent chunks to each result (batch, single DB call)."""
        if not results or window <= 0:
            return results

        source_files = list({r.chunk.metadata.source_file for r in results})
        chunks_by_source = await self._storage.list_chunks_by_sources(source_files)

        # Build per-file index: {chunk_id -> position}
        file_indexes: dict[str, dict[str, int]] = {}
        for sf, chunks in chunks_by_source.items():
            file_indexes[str(sf)] = {str(c.id): i for i, c in enumerate(chunks)}

        expanded: list[SearchResult] = []
        for r in results:
            sf_key = str(r.chunk.metadata.source_file)
            idx_map = file_indexes.get(sf_key)
            if idx_map is None:
                expanded.append(r)
                continue
            pos = idx_map.get(str(r.chunk.id))
            if pos is None:
                expanded.append(r)
                continue

            file_chunks = chunks_by_source[r.chunk.metadata.source_file]
            before = file_chunks[max(0, pos - window) : pos]
            after = file_chunks[pos + 1 : pos + 1 + window]

            expanded.append(
                SearchResult(
                    chunk=r.chunk,
                    score=r.score,
                    rank=r.rank,
                    source=r.source,
                    context=ContextInfo(
                        window_before=tuple(before),
                        window_after=tuple(after),
                        chunk_position=pos + 1,
                        total_chunks_in_file=len(file_chunks),
                        context_tier_used="standard",
                    ),
                )
            )
        return expanded

    async def search(
        self,
        query: str,
        top_k: int | None = None,
        source_filter: str | None = None,
        tag_filter: str | None = None,
        namespace: str | list[str] | None = None,
        rrf_weights: list[float] | None = None,
        context_window: int | None = None,
    ) -> tuple[list[SearchResult], RetrievalStats]:
        top_k = self._config.default_top_k if top_k is None else top_k
        effective_weights = rrf_weights or self._config.rrf_weights

        # Check TTL cache for identical queries
        import time

        cache_key = self._cache_key(
            query, top_k, source_filter, tag_filter, namespace, context_window
        )
        version_at_start = self._cache_version
        ttl_snapshot = self._cache_ttl
        if cache_key in self._search_cache:
            ts, ver, cached_results, cached_stats = self._search_cache[cache_key]
            if ver == self._cache_version and time.time() - ts < ttl_snapshot:
                return cached_results, cached_stats
            self._search_cache.pop(cache_key, None)

        bm25_k = max(self._config.bm25_candidates, top_k)
        dense_k = max(self._config.dense_candidates, top_k)
        ns_filter = NamespaceFilter.parse(
            namespace,
            system_prefixes=tuple(self._config.system_namespace_prefixes),
        )

        # When the caller did not pin a namespace, count how many chunks sit
        # behind a system-namespace prefix (e.g. archive:*) so the tool layer
        # can hint "N hidden — pass namespace=... to include them".
        hidden_system_ns = 0
        if namespace is None and self._config.system_namespace_prefixes:
            try:
                hidden_system_ns = await self._storage.count_chunks_by_ns_prefix(
                    list(self._config.system_namespace_prefixes)
                )
            except Exception:
                logger.debug("count_chunks_by_ns_prefix failed; skipping hint", exc_info=True)

        use_bm25 = self._config.enable_bm25
        use_dense = self._config.enable_dense

        # Stage 0: Query expansion
        if self._expansion_config and getattr(self._expansion_config, "enabled", False):
            from memtomem.search.expansion import (
                expand_query_headings,
                expand_query_llm,
                expand_query_tags,
            )

            strategy = getattr(self._expansion_config, "strategy", "tags")
            max_terms = getattr(self._expansion_config, "max_terms", 3)
            if strategy in ("tags", "both"):
                query = await expand_query_tags(query, self._storage, max_terms)
            if strategy in ("headings", "both"):
                query = await expand_query_headings(query, self._storage, self._embedder, max_terms)
            if strategy == "llm":
                if query in self._expansion_cache:
                    query = self._expansion_cache[query]
                elif self._llm_provider is not None:
                    try:
                        original = query
                        query = await expand_query_llm(
                            query,
                            self._llm_provider,
                            max_terms,  # type: ignore[arg-type]
                        )
                        if len(self._expansion_cache) >= _EXPANSION_CACHE_MAX:
                            self._expansion_cache.clear()
                        self._expansion_cache[original] = query
                    except Exception:
                        logger.warning(
                            "LLM query expansion failed, using original query",
                            exc_info=True,
                        )

        # Stage 1 + 2: run enabled retrievers concurrently
        bm25_results: list[SearchResult] = []
        dense_results: list[SearchResult] = []
        query_embedding: list[float] = []
        bm25_error: str | None = None

        if use_bm25:
            bm25_task = asyncio.create_task(
                self._storage.bm25_search(query, top_k=bm25_k, namespace_filter=ns_filter)
            )
        dense_error: str | None = None
        if use_dense:
            try:
                query_embedding = await self._embedder.embed_query(query)
                dense_results = await self._storage.dense_search(
                    query_embedding, top_k=dense_k, namespace_filter=ns_filter
                )
            except Exception as exc:
                logger.warning("Dense search unavailable: %s", exc)
                dense_results = []
                dense_error = str(exc)
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
            dense_error=dense_error,
            hidden_system_ns=hidden_system_ns,
        )

        # Stage 3: fusion (or single-retriever passthrough)
        # When reranking is active, widen the candidate pool so the
        # cross-encoder can rescue items RRF ranked just outside top_k.
        # pool = clamp(oversample * top_k, [min_pool, max_pool]) — scales
        # with the request and bounded by cost controls. Collapses to
        # top_k when reranking is disabled so single-retriever passthrough
        # size is unchanged.
        if self._reranker is not None and self._rerank_config is not None:
            rcfg = self._rerank_config
            rerank_pool = max(
                rcfg.min_pool,
                min(rcfg.max_pool, int(rcfg.oversample * top_k)),
            )
        else:
            rerank_pool = top_k

        if use_bm25 and use_dense:
            fused = reciprocal_rank_fusion(
                [bm25_results, dense_results],
                k=self._config.rrf_k,
                top_k=rerank_pool,
                weights=effective_weights,
            )
        elif use_bm25:
            fused = bm25_results[:rerank_pool]
        elif use_dense:
            fused = dense_results[:rerank_pool]
        else:
            fused = []
        stats.fused_total = len(fused)

        # Stage 3b: Cross-encoder reranking
        if self._reranker is not None and fused:
            try:
                fused = await self._reranker.rerank(query, fused, top_k=top_k)
            except Exception as exc:
                logger.warning("Reranking failed, using original order: %s", exc)
                # Fallback must still honor the caller's response size —
                # fused is at rerank_pool (e.g. 20) right now, not top_k.
                fused = fused[:top_k]

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

            access_chunk_ids = [r.chunk.id for r in fused]
            access_counts = await self._storage.get_access_counts(access_chunk_ids)
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

        # Stage 8: Context window expansion (post-scoring, does not affect ranking)
        ctx_win = self._resolve_context_window(context_window)
        if ctx_win > 0 and fused:
            fused = await self._expand_context(fused, ctx_win)

        stats.final_total = len(fused)

        # Increment access counts for returned results (fire-and-forget)
        if fused:

            async def _increment():
                await self._storage.increment_access([r.chunk.id for r in fused])

            t = asyncio.create_task(_increment())
            t.add_done_callback(_bg_task_error_cb)
            self._bg_tasks.add(t)
            t.add_done_callback(self._bg_tasks.discard)

        # Save to query history (fire-and-forget)
        async def _save_history():
            emb = query_embedding if use_dense else []
            await self._storage.save_query_history(
                query,
                emb,
                [str(r.chunk.id) for r in fused[:top_k]],
                [r.score for r in fused[:top_k]],
            )

        t2 = asyncio.create_task(_save_history())
        t2.add_done_callback(_bg_task_error_cb)
        self._bg_tasks.add(t2)
        t2.add_done_callback(self._bg_tasks.discard)

        # Store in TTL cache only if version hasn't changed during search
        if self._cache_version == version_at_start:
            self._search_cache[cache_key] = (time.time(), version_at_start, fused, stats)
            # Evict old entries (keep max 50)
            if len(self._search_cache) > 50:
                try:
                    oldest_key = min(self._search_cache, key=lambda k: self._search_cache[k][0])
                    self._search_cache.pop(oldest_key, None)
                except ValueError:
                    pass  # cache emptied by concurrent invalidate_cache()

        return fused, stats

    async def close(self) -> None:
        """Release resources held by the pipeline (reranker client, etc.)."""
        if self._bg_tasks:
            await asyncio.gather(*self._bg_tasks, return_exceptions=True)
            self._bg_tasks.clear()
        if self._reranker is not None and hasattr(self._reranker, "close"):
            await self._reranker.close()
