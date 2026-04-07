"""Proactive memory surfacing engine."""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING, Any
from uuid import UUID

from memtomem_stm.surfacing.cache import SurfacingCache
from memtomem_stm.surfacing.config import SurfacingConfig
from memtomem_stm.surfacing.context_extractor import ContextExtractor
from memtomem_stm.surfacing.formatter import SurfacingFormatter
from memtomem_stm.surfacing.relevance import RelevanceGate
from memtomem_stm.utils.circuit_breaker import CircuitBreaker

if TYPE_CHECKING:
    from memtomem.search.pipeline import SearchPipeline
    from memtomem.storage.sqlite_backend import SqliteBackend

logger = logging.getLogger(__name__)


class SurfacingEngine:
    """Core proactive memory surfacing engine.

    On each proxied tool call, extracts context, searches LTM,
    and injects relevant memories into the response.
    """

    def __init__(
        self,
        config: SurfacingConfig,
        search_pipeline: SearchPipeline | None = None,
        storage: SqliteBackend | None = None,
        webhook_manager: Any | None = None,
        mcp_adapter: Any | None = None,
        feedback_tracker: Any | None = None,
    ) -> None:
        self._config = config
        self._search_pipeline = search_pipeline
        self._mcp_adapter = mcp_adapter
        self._storage = storage
        self._webhook_manager = webhook_manager
        self._feedback_tracker = feedback_tracker
        self._auto_tuner = None
        if config.auto_tune_enabled and feedback_tracker is not None:
            from memtomem_stm.surfacing.feedback import AutoTuner

            self._auto_tuner = AutoTuner(config, feedback_tracker.store)
        self._extractor = ContextExtractor()
        self._gate = RelevanceGate(config)
        self._formatter = SurfacingFormatter(config)
        self._cache = SurfacingCache(ttl=config.cache_ttl_seconds)
        self._circuit_breaker = CircuitBreaker(
            max_failures=config.circuit_max_failures,
            reset_timeout=config.circuit_reset_seconds,
        )
        # Track memory IDs surfaced — seeded from persistent store for cross-session dedup
        self._surfaced_ids: set[str] = set()
        if feedback_tracker is not None and config.dedup_ttl_seconds > 0:
            try:
                self._surfaced_ids = feedback_tracker.store.get_seen_ids(
                    config.dedup_ttl_seconds
                )
                if self._surfaced_ids:
                    logger.debug(
                        "Loaded %d seen memory IDs for cross-session dedup",
                        len(self._surfaced_ids),
                    )
            except Exception:
                logger.debug("Failed to load cross-session seen IDs", exc_info=True)
        # Track surfacing IDs already boosted to prevent double-counting
        self._boosted_surfacings: set[str] = set()

    async def surface(
        self,
        server: str,
        tool: str,
        arguments: dict[str, Any],
        response_text: str,
    ) -> str:
        """Surface relevant memories and inject into response_text.

        Returns the original response_text unmodified if:
        - surfacing is disabled
        - no search pipeline available
        - circuit breaker is open
        - relevance gate rejects the call
        - timeout exceeded
        """
        if not self._config.enabled or (
            self._search_pipeline is None and self._mcp_adapter is None
        ):
            return response_text

        if len(response_text) < self._config.min_response_chars:
            return response_text

        if self._circuit_breaker.is_open:
            logger.debug("Surfacing skipped: circuit breaker open for %s/%s", server, tool)
            return response_text

        query = self._extractor.extract_query(server, tool, arguments, self._config)
        if not self._gate.should_surface(server, tool, query):
            logger.debug(
                "Surfacing skipped: gate rejected %s/%s (query=%s)",
                server,
                tool,
                query[:50] if query else None,
            )
            return response_text

        try:
            result = await asyncio.wait_for(
                self._do_surface(server, tool, arguments, response_text, query),
                timeout=self._config.timeout_seconds,
            )
            self._circuit_breaker.record_success()
            return result
        except asyncio.TimeoutError:
            logger.warning(
                "Surfacing timed out for %s/%s (%.1fs limit)",
                server,
                tool,
                self._config.timeout_seconds,
            )
            return response_text
        except Exception:
            logger.warning("Surfacing failed for %s/%s", server, tool, exc_info=True)
            self._circuit_breaker.record_failure()
            return response_text

    async def handle_feedback(
        self,
        surfacing_id: str,
        rating: str,
        memory_id: str | None = None,
    ) -> str:
        """Record feedback and boost access count for helpful memories.

        When rating is 'helpful', increments access_count on the surfaced
        memory chunks in the core storage. This feeds into the search
        pipeline's access-frequency boost, making helpful memories rank
        higher in future searches.
        """
        if self._feedback_tracker is None:
            return "Feedback tracking is not enabled."

        result = self._feedback_tracker.record_feedback(surfacing_id, rating, memory_id)

        # Boost access count for helpful memories (once per surfacing event)
        if (
            rating == "helpful"
            and self._storage is not None
            and surfacing_id not in self._boosted_surfacings
        ):
            try:
                if memory_id:
                    ids = [UUID(memory_id)]
                else:
                    id_strs = self._feedback_tracker.store.get_memory_ids_for_surfacing(
                        surfacing_id
                    )
                    ids = [UUID(mid) for mid in id_strs]
                if ids:
                    await self._storage.increment_access(ids)
                    self._boosted_surfacings.add(surfacing_id)
                    logger.debug(
                        "Boosted access count for %d memories (surfacing %s)",
                        len(ids),
                        surfacing_id,
                    )
            except Exception:
                logger.debug("Failed to boost access for feedback", exc_info=True)

        return result

    async def _do_surface(
        self,
        server: str,
        tool: str,
        arguments: dict[str, Any],
        response_text: str,
        query: str,
    ) -> str:
        # Check surfacing cache (keyed by server+tool+query)
        cache_key = f"{server}/{tool}/{query}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            if not cached:
                logger.debug("Surfacing cache hit (empty) for %s/%s", server, tool)
                return response_text
            logger.debug("Surfacing cache hit (%d results) for %s/%s", len(cached), server, tool)
            surfacing_id = uuid.uuid4().hex[:12]
            return self._formatter.inject(
                response_text,
                cached,
                query,
                surfacing_id=surfacing_id,
            )

        # Resolve effective config (auto-tuned if enabled)
        tool_cfg = self._config.context_tools.get(tool)
        if self._auto_tuner is not None:
            self._auto_tuner.maybe_adjust(tool)
            min_score = self._auto_tuner.get_effective_min_score(tool)
        else:
            min_score = (
                tool_cfg.min_score if tool_cfg and tool_cfg.min_score else self._config.min_score
            )
        max_results = (
            tool_cfg.max_results if tool_cfg and tool_cfg.max_results else self._config.max_results
        )
        namespace = (
            tool_cfg.namespace
            if tool_cfg and tool_cfg.namespace
            else self._config.default_namespace
        )

        # Search LTM (in-process or MCP client)
        searcher = self._search_pipeline or self._mcp_adapter
        results, _stats = await searcher.search(
            query=query,
            top_k=max_results * 2,
            namespace=namespace,
        )

        # Filter by score, then exclude already-surfaced memories in this session
        scored = [r for r in results if r.score >= min_score]
        relevant = []
        for r in scored:
            mid = str(r.chunk.id)
            if mid not in self._surfaced_ids:
                relevant.append(r)
                if len(relevant) >= max_results:
                    break

        # Cache result (even empty, to avoid repeated searches)
        self._cache.set(cache_key, relevant)

        if not relevant:
            logger.debug(
                "Surfacing: no results above min_score=%.2f for %s/%s", min_score, server, tool
            )
            return response_text

        logger.info(
            "Surfacing %d memories for %s/%s (query=%s)", len(relevant), server, tool, query[:50]
        )

        # Session context (working memory)
        scratch_items = None
        if self._config.include_session_context and self._storage:
            try:
                scratch_items = await self._storage.scratch_list()
            except Exception:
                logger.debug("Failed to fetch scratch items", exc_info=True)

        # Generate surfacing ID and record event
        surfacing_id = uuid.uuid4().hex[:12]
        if self._feedback_tracker is not None:
            try:
                self._feedback_tracker.record_surfacing(
                    surfacing_id=surfacing_id,
                    server=server,
                    tool=tool,
                    query=query,
                    memory_ids=[str(r.chunk.id) for r in relevant],
                    scores=[r.score for r in relevant],
                )
            except Exception:
                logger.debug("Failed to record surfacing event", exc_info=True)

        # Record surfaced IDs to suppress repeats (in-memory + persistent)
        new_ids = [str(r.chunk.id) for r in relevant]
        for mid in new_ids:
            self._surfaced_ids.add(mid)
        if self._feedback_tracker is not None:
            try:
                self._feedback_tracker.store.mark_surfaced(new_ids)
            except Exception:
                logger.debug("Failed to persist seen memory IDs", exc_info=True)

        # Inject memories into response
        result = self._formatter.inject(
            response_text,
            relevant,
            query,
            surfacing_id=surfacing_id,
            scratch_items=scratch_items,
        )

        # Fire webhook (fire-and-forget)
        if self._webhook_manager and self._config.fire_webhook:
            asyncio.create_task(
                self._webhook_manager.fire(
                    "surface",
                    {
                        "server": server,
                        "tool": tool,
                        "query": query,
                        "memory_ids": [str(r.chunk.id) for r in relevant],
                        "scores": [r.score for r in relevant],
                        "surfacing_id": surfacing_id,
                    },
                )
            )

        return result
