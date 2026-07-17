"""Search pipeline: BM25 + Dense + RRF fusion.

Stage order (keyword path, fixed — see ``CLAUDE.md`` invariants):
expansion → BM25 + dense (parallel, with always-on scope-context filter
per ADR-0011 §6) → RRF fusion → cross-encoder rerank (optional) →
source/tag filter → validity filter → time-decay → MMR → access-freq
boost → importance boost → context-window expansion.

Stage 1 enrichment (session-summary rescue, RFC P1 Phase C): between
retrieval and fusion, when ``namespace is None`` and a
``SessionSummaryConfig`` with ``expansion_lookup_top_k > 0`` is wired,
an ``archive:session:*`` summary lookup + ``summarizes`` chunk-links
walk builds a boost-source set and a retrieval restricted to those
sources joins RRF as a third input list weighted by
``expansion_rescue_weight``. Failures degrade to two-leg fusion (loud
once — see ``_log_rescue_failure``).

The scope-context filter is applied **at the BM25 + dense storage
calls** (not as a post-fusion stage) because the SQL fragment is the
single chokepoint that prevents cross-project leak; running it at the
storage layer rather than after fusion guarantees no candidate ever
reaches the pipeline that the project-context boundary would have
excluded. Tie-break ordering ``project_local > project_shared > user``
applies at the same site (storage ORDER BY) so same-relevance ranks
surface freshest-context-first.

Empty-query path (``query=""`` / ``None`` with ``tag_filter`` /
``source_filter`` set, #750) routes through ``_filter_only_search``
and skips expansion / BM25 / dense / RRF / rerank / MMR — none of
those have a meaningful signal without a query. Validity → decay →
access → importance → context-window still apply so the rank
reflects recency × access × importance. The scope-context filter
applies in this branch too via ``recall_chunks``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from fnmatch import fnmatch
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from dataclasses import dataclass

from dataclasses import replace as dataclass_replace
from uuid import UUID, uuid4

from memtomem.config import (
    MAX_CONTEXT_WINDOW_CHUNKS,
    AccessConfig,
    ContextWindowConfig,
    DecayConfig,
    MMRConfig,
    RerankConfig,
    SearchConfig,
    SessionSummaryConfig,
)
from memtomem.models import ContextInfo, NamespaceFilter, ScopeFilter, SearchResult
from memtomem.search.fusion import reciprocal_rank_fusion
from memtomem.search.reranker.base import close_reranker_safely
from memtomem.storage.base import SearchMetadataFilter
from memtomem.storage.sqlite_helpers import norm_path

logger = logging.getLogger(__name__)

# Fallback for the rescue leg's RRF weight when no SessionSummaryConfig
# is wired. Mirrors ``SessionSummaryConfig.expansion_rescue_weight``'s
# default (config.py) — keep the two in sync.
_DEFAULT_RESCUE_WEIGHT = 0.5

# Rescue-leg failures silently degrade search to two-leg fusion, which
# is invisible in production — loud (warning, not debug) on the first
# occurrence per site per feedback_silent_except_log_level (reference
# pattern: storage/mixins/schedules.py), DEBUG afterwards so a
# persistently failing dependency doesn't spam the log on every query.
_RESCUE_WARNED: set[str] = set()


def _log_rescue_failure(site: str, msg: str, *args: object) -> None:
    """warn-once-per-process logger for rescue-leg swallow sites."""
    level = logging.DEBUG if site in _RESCUE_WARNED else logging.WARNING
    _RESCUE_WARNED.add(site)
    logger.log(level, msg, *args, exc_info=True)


# MMR-without-dense is a config mismatch that silently disables diversity
# re-ranking (#1619) — log once per process, DEBUG afterwards, mirroring
# the rescue-leg pattern above.
_MMR_NO_DENSE_WARNED = False


def _log_mmr_no_dense_once() -> None:
    global _MMR_NO_DENSE_WARNED
    level = logging.DEBUG if _MMR_NO_DENSE_WARNED else logging.INFO
    _MMR_NO_DENSE_WARNED = True
    logger.log(
        level,
        "MMR diversity re-ranking is enabled (mmr.enabled=True) but dense "
        "retrieval is off (search.enable_dense=False) — MMR is skipped. "
        "mem_status reports this as a `mmr_disabled_no_dense` warning.",
    )


def _bg_task_error_cb(task: asyncio.Task) -> None:
    """Log errors from fire-and-forget background tasks."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        logger.warning("Background task %s failed: %s", task.get_name(), exc)


class _RerankerEntry:
    """One (reranker, config) generation with an in-flight lease count (#1777).

    Identity-hashed on purpose: retired generations live in a set keyed by
    the entry object itself, so two generations wrapping the same values
    never collide.
    """

    __slots__ = ("reranker", "config", "leases")

    def __init__(self, reranker: object | None, config: RerankConfig | None) -> None:
        self.reranker = reranker
        self.config = config
        self.leases = 0


def match_source_filter(filter_str: str, source_path: str) -> bool:
    """Match source_filter: glob when pattern chars present, substring otherwise.

    Both sides are folded to forward-slash form before comparing so a
    user-typed ``/tmp/keep/`` matches a Windows-stored
    ``\\tmp\\keep\\file.md`` (#720, sibling of #647). On POSIX
    ``str.replace("\\", "/")`` is a no-op for the typical case where
    paths don't contain backslashes.

    The ``source_filter`` parameter is shared across the search pipeline
    and several MCP tools (``mem_list``, ``mem_consolidate``); this
    helper is the canonical matcher for callers whose contract is
    "substring or glob, autodetected by pattern chars". Callers with a
    stricter contract use one of the two siblings below
    (``match_source_filter_substring``, ``match_source_filter_glob``)
    so the separator-fold rule still lives in one module per contract
    while the contract differences stay explicit at the call site.

    POSIX edge case: backslash is a legal filename character on POSIX, so
    a chunk indexed under ``foo\\bar.md`` would match a filter
    ``foo/bar`` after the fold. This is rare in practice (most tools and
    users avoid backslashes in POSIX filenames) and is the trade-off for
    a Windows-portable comparison.
    """
    norm_filter = filter_str.replace("\\", "/")
    norm_source = source_path.replace("\\", "/")
    if any(c in norm_filter for c in ("*", "?", "[")):
        return fnmatch(norm_source, norm_filter)
    return norm_filter in norm_source


def match_source_filter_substring(filter_str: str, source_path: str) -> bool:
    """Substring-only variant of :func:`match_source_filter`.

    Same separator-fold rule (#720), no glob fallback. Used by callers
    whose contract is substring-only (``mem_decay`` /
    :func:`~memtomem.search.decay.expire_chunks`, ``mem_auto_tag``,
    ``mem_export_chunks``). Sharing the substring + glob auto-detecting
    helper would silently broaden their behaviour to glob, which is a
    contract change. The fold lives in this single helper so a future
    "tidy-up" that strips the ``.replace("\\", "/")`` calls fails the
    POSIX-runnable pin in ``TestMatchSourceFilterSubstring`` instead of
    only the Windows CI leg.
    """
    return filter_str.replace("\\", "/") in source_path.replace("\\", "/")


def match_source_filter_glob(filter_str: str, source_path: str) -> bool:
    """Glob-only variant of :func:`match_source_filter`.

    Same separator-fold rule (#720), no substring fallback. Used by
    callers whose contract is glob-only (``mem_entity_scan``); a
    substring filter that lacks ``*?[`` characters returns ``False``
    here unless it happens to be an exact-match glob, mirroring the
    pre-fix behaviour. The fold lives in this single helper so the
    POSIX-runnable pin in ``TestMatchSourceFilterGlob`` catches a
    revert without depending on the Windows CI leg.
    """
    return fnmatch(source_path.replace("\\", "/"), filter_str.replace("\\", "/"))


def _matches_metadata(result: SearchResult, metadata_filter: SearchMetadataFilter | None) -> bool:
    if metadata_filter is None:
        return True
    chunk = result.chunk
    if metadata_filter.source_exact:
        allowed = {norm_path(Path(value)) for value in metadata_filter.source_exact}
        if norm_path(chunk.metadata.source_file) not in allowed:
            return False
    if metadata_filter.chunk_types:
        if str(chunk.metadata.chunk_type) not in set(metadata_filter.chunk_types):
            return False
    created_at = chunk.created_at
    if created_at.tzinfo is None:
        # Legacy/imported rows may carry an offset-less ISO timestamp. The
        # storage contract has always treated those values as UTC, so preserve
        # that meaning before comparing with the route's aware UTC bounds.
        created_at = created_at.replace(tzinfo=UTC)
    else:
        created_at = created_at.astimezone(UTC)
    if metadata_filter.created_from is not None and created_at < metadata_filter.created_from:
        return False
    if metadata_filter.created_before is not None and created_at >= metadata_filter.created_before:
        return False
    return True


def _normalize_source_roots(roots: tuple[Path, ...] | None) -> tuple[Path, ...]:
    if not roots:
        return ()
    return tuple(sorted({root.expanduser().resolve(strict=False) for root in roots}, key=str))


def _source_is_excluded(source_file: Path, roots: tuple[Path, ...]) -> bool:
    if not roots:
        return False
    source = Path(source_file).expanduser().resolve(strict=False)
    return any(source == root or source.is_relative_to(root) for root in roots)


def _exclude_source_roots(
    results: list[SearchResult], roots: tuple[Path, ...]
) -> list[SearchResult]:
    if not roots:
        return results
    return [r for r in results if not _source_is_excluded(r.chunk.metadata.source_file, roots)]


def _apply_validity_filter(results: list[SearchResult], as_of_unix: int) -> list[SearchResult]:
    """Drop chunks whose temporal-validity window excludes ``as_of_unix``.

    Inclusive on both ends (``valid_from <= as_of <= valid_to``); ``None`` on
    a bound means unbounded on that side; both ``None`` means always-valid
    (RFC §Comparison semantics — opt-in default for chunks without a window).

    Order is preserved. The function returns a new list so callers can
    chain it after source/tag filter without mutating the input.

    Granularity note: when the pipeline falls back to the default
    ``int(time.time())``, results may be served from the search-result TTL
    cache for up to ``cache_ttl`` seconds. A chunk whose window expires at
    midnight can therefore continue to surface for the cache-TTL window
    after expiry. This is acceptable because the RFC's date-only bounds
    already operate at 24h granularity; sub-minute drift is invisible at
    that resolution.
    """
    filtered: list[SearchResult] = []
    for r in results:
        vfrom = r.chunk.metadata.valid_from_unix
        vto = r.chunk.metadata.valid_to_unix
        if vfrom is None and vto is None:
            filtered.append(r)
            continue
        lower = vfrom if vfrom is not None else float("-inf")
        upper = vto if vto is not None else float("inf")
        if lower <= as_of_unix <= upper:
            filtered.append(r)
    return filtered


# Closed vocabulary for ``RetrievalStats.score_scale`` (#1767) — typing the
# field makes a misspelled label fail at the assignment site instead of
# escaping into structured output.
ScoreScale = Literal["rerank", "rrf", "bm25", "dense", "none"]


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
    # Whether Stage 3b cross-encoder reranking actually ran for this call —
    # the per-call effective decision (#1766), not the live server config,
    # so hint builders stay accurate across concurrent hot reloads.
    rerank_applied: bool = False
    # Which BASE scale the returned ``score`` values are on (#1767):
    # "rerank" (cross-encoder output — range is model-dependent, see
    # ``reranker_model``), "rrf" (reciprocal-rank fusion, bounded near
    # ``legs/rrf_k``), "bm25"/"dense" (single-retriever passthrough:
    # unfused BM25 / cosine scores), "none" (filter-only enumeration —
    # no relevance scale, the filter is the selector), or None (no
    # scoring path taken, e.g. both retrievers disabled; a taken path
    # keeps its scale even when it yielded zero results — public
    # formatters omit the key for empty responses either way). Base
    # means pre-modifier: decay / access /
    # importance boosts (Stages 4/6/7, all default-off) multiply on top
    # when enabled, so absolute thresholds are only portable across
    # servers with the same modifier config. Unlike ``rerank_applied``
    # (the pre-Stage-3b decision), this is derived from the results
    # actually returned, so a reranker that fails and silently falls back
    # to the fused order keeps the label truthful.
    score_scale: ScoreScale | None = None
    # Rerank model identifier, set only when ``score_scale == "rerank"`` —
    # rerank ranges differ per provider (local cross-encoders emit raw
    # logits, Cohere emits [0, 1] relevance), so clients calibrating a
    # threshold need the model, not just the scale family.
    reranker_model: str | None = None
    # Quality Lab observation envelope. ``query_run_id`` is present only when
    # the local history commit succeeded; search availability never depends on
    # observation persistence.
    query_run_id: str | None = None
    cache_hit: bool = False
    latency_ms: float | None = None


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
        session_summary_config: SessionSummaryConfig | None = None,
    ) -> None:
        self._storage = storage
        self._embedder = embedder
        self._config = config
        self._decay_config = decay_config or DecayConfig()
        self._mmr_config = mmr_config or MMRConfig()
        self._access_config = access_config or AccessConfig()
        self._rerank_entry = _RerankerEntry(reranker, rerank_config)
        self._retired_rerank_entries: set[_RerankerEntry] = set()
        self._expansion_config = expansion_config
        self._importance_config = importance_config
        self._context_window_config = context_window_config
        self._llm_provider = llm_provider
        self._session_summary_config = session_summary_config

        # Search result TTL cache (per-instance) with version counter
        self._search_cache: dict[str, tuple[float, int, list[SearchResult], RetrievalStats]] = {}
        self._cache_ttl = config.cache_ttl
        self._cache_version = 0
        self._bg_tasks: set[asyncio.Task] = set()

        # LLM query expansion cache (cleared on invalidate_cache)
        self._expansion_cache: dict[str, str] = {}

    async def _record_ranked_search(
        self,
        *,
        query: str,
        query_embedding: list[float],
        results: list[SearchResult],
        stats: RetrievalStats,
        top_k: int,
        origin: str,
        namespace: str | list[str] | None,
        scope: str | list[str] | None,
        source_filter: str | None,
        tag_filter: str | None,
        metadata_filter: SearchMetadataFilter | None,
        as_of_unix: int | None,
        rrf_weights: list[float],
    ) -> str | None:
        """Persist a content-minimized ranked-search observation when supported.

        Alternate storage backends that only implement legacy query history
        keep the old fire-and-forget behavior and receive no public run ID.
        """
        # Avoid ``getattr(instance, ...)`` as the capability probe: dynamic
        # mocks/proxies may fabricate any attribute. A real class method or an
        # explicitly attached instance method counts as support, and the same
        # bound callable is then used for dispatch.
        class_saver = getattr(type(self._storage), "save_search_observation", None)
        instance_has_saver = "save_search_observation" in vars(self._storage)
        saver = (
            getattr(self._storage, "save_search_observation")
            if class_saver is not None or instance_has_saver
            else None
        )
        if saver is None:
            # Preserve the pre-Quality-Lab contract for alternate backends:
            # cache hits returned before scheduling legacy history writes.
            if stats.cache_hit:
                return None

            async def _save_legacy_history() -> None:
                await self._storage.save_query_history(
                    query,
                    query_embedding,
                    [str(result.chunk.id) for result in results[:top_k]],
                    [result.score for result in results[:top_k]],
                )

            task = asyncio.create_task(_save_legacy_history())
            task.add_done_callback(_bg_task_error_cb)
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)
            return None

        normalized_origin = (
            origin
            if origin in {"web", "cli", "mcp", "shell", "langgraph", "internal"}
            else "internal"
        )
        if any("\uac00" <= char <= "\ud7a3" for char in query):
            query_language = "ko"
        elif any(char.isascii() and char.isalpha() for char in query):
            query_language = "en"
        else:
            query_language = "other"

        profile: dict[str, object] = {
            "bm25_enabled": self._config.enable_bm25,
            "dense_enabled": self._config.enable_dense,
            "bm25_candidates": self._config.bm25_candidates,
            "dense_candidates": self._config.dense_candidates,
            "rrf_weights": list(rrf_weights),
            "embedding_provider": type(self._embedder).__name__,
            "embedding_model": getattr(self._embedder, "model_name", None),
            "embedding_dimension": self._embedder.dimension,
            "rerank_applied": stats.rerank_applied,
            "reranker": stats.reranker_model,
        }
        canonical_profile = json.dumps(
            profile, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        observation: dict[str, object] = {
            "origin": normalized_origin,
            "purpose": "search",
            "query_language": query_language,
            "top_k": top_k,
            "filters": {
                "namespace": namespace,
                "scope": scope,
                "has_source_filter": source_filter is not None,
                "has_tag_filter": tag_filter is not None,
                "has_metadata_filter": metadata_filter is not None,
                "has_as_of": as_of_unix is not None,
            },
            "cache_hit": stats.cache_hit,
            "latency_ms": stats.latency_ms,
            "score_scale": stats.score_scale,
            "reranker": stats.reranker_model,
            "bm25_candidates": stats.bm25_candidates,
            "dense_candidates": stats.dense_candidates,
            "final_total": stats.final_total,
            "bm25_degraded": stats.bm25_error is not None,
            "dense_degraded": stats.dense_error is not None,
            "profile_id": hashlib.sha256(canonical_profile.encode()).hexdigest(),
            "profile": profile,
        }
        result_snapshot = [
            {
                "chunk_id": str(result.chunk.id),
                "rank": result.rank,
                "score": result.score,
                "source_name": result.chunk.metadata.source_file.name,
                "content_hash": result.chunk.content_hash,
                "heading_hierarchy": list(result.chunk.metadata.heading_hierarchy),
                "namespace": result.chunk.metadata.namespace,
                "language": result.chunk.metadata.language,
            }
            for result in results[:top_k]
        ]
        run_id = str(uuid4())
        try:
            return await saver(
                query,
                query_embedding,
                [str(result.chunk.id) for result in results[:top_k]],
                [result.score for result in results[:top_k]],
                run_id=run_id,
                observation=observation,
                result_snapshot=result_snapshot,
            )
        except Exception:
            logger.debug("search observation persistence failed", exc_info=True)
            return None

    @property
    def rerank_active(self) -> bool:
        """True when a reranker instance is wired and configured (server-side on).

        Hot reload keeps the pair consistent: the two always live on one
        ``_RerankerEntry`` generation, swapped atomically by
        :meth:`swap_reranker`.
        """
        return self._reranker is not None and self._rerank_config is not None

    @property
    def _reranker(self) -> object | None:
        return self._rerank_entry.reranker

    @_reranker.setter
    def _reranker(self, value: object | None) -> None:
        # Direct assignment installs a fresh generation and abandons the old
        # one WITHOUT closing it (the assigner owns its lifecycle) — exactly
        # the pre-#1777 contract. Hot-reload paths must use swap_reranker.
        # getattr: the plain-attribute contract allowed assignment on a bare
        # instance (tests build one via __new__ to probe _cache_key).
        prev = getattr(self, "_rerank_entry", None)
        self._rerank_entry = _RerankerEntry(value, prev.config if prev else None)

    @property
    def _rerank_config(self) -> RerankConfig | None:
        return self._rerank_entry.config

    @_rerank_config.setter
    def _rerank_config(self, value: RerankConfig | None) -> None:
        # See the _reranker setter: fresh generation, old one abandoned unclosed.
        prev = getattr(self, "_rerank_entry", None)
        self._rerank_entry = _RerankerEntry(prev.reranker if prev else None, value)

    @contextlib.contextmanager
    def _lease_reranker(self) -> Iterator[tuple[object | None, RerankConfig | None]]:
        """Lease the current reranker generation for the duration of a search.

        Acquire/release are synchronous ops between awaits on one event
        loop, so a plain counter suffices — no lock. A generation retired
        by :meth:`swap_reranker` while leased is closed here, on its last
        release, as a background task so the unlucky last search doesn't
        pay the close latency (``close()`` gathers ``_bg_tasks``).
        """
        entry = self._rerank_entry
        entry.leases += 1
        try:
            yield entry.reranker, entry.config
        finally:
            entry.leases -= 1
            if entry.leases == 0 and entry in self._retired_rerank_entries:
                # Popping synchronously before scheduling is the once-only
                # latch: no other release (or close()) can see the entry.
                self._retired_rerank_entries.discard(entry)
                t = asyncio.create_task(close_reranker_safely(entry.reranker))
                t.add_done_callback(_bg_task_error_cb)
                self._bg_tasks.add(t)
                t.add_done_callback(self._bg_tasks.discard)

    async def swap_reranker(self, reranker: object | None, config: RerankConfig | None) -> None:
        """Atomically install a new (reranker, config) generation (#1777).

        The single hot-swap API for the web hot-reload and PATCH paths.
        Publishes the new pair before any await — a hanging close still
        leaves the pipeline on the new reranker. Closes the old instance
        immediately when no in-flight search leases it; otherwise defers
        the close to the last lease release.

        Contract: ``reranker`` must be ``None`` or a fresh instance never
        previously installed. Re-installing the live instance early-returns
        below WITHOUT retiring the old generation, so its leases no longer
        guard the instance — a later swap would see zero leases on the new
        generation and close it out from under an in-flight search. Both
        production call sites always pass a fresh ``create_reranker``
        product or ``None``; supporting identity re-install would take a
        per-instance (not per-generation) refcount.
        """
        old = self._rerank_entry
        self._rerank_entry = _RerankerEntry(reranker, config)
        if old.reranker is None or old.reranker is reranker:
            return
        if old.leases == 0:
            await close_reranker_safely(old.reranker)
        else:
            self._retired_rerank_entries.add(old)

    def _cache_key(
        self,
        query: str,
        top_k: int,
        source_filter: str | None,
        tag_filter: str | None,
        namespace: str | list[str] | None,
        context_window: int | None = None,
        scope: str | list[str] | None = None,
        project_context_root: Path | None = None,
        metadata_filter: SearchMetadataFilter | None = None,
        exclude_source_roots: tuple[Path, ...] = (),
        effective_rrf_weights: list[float] | tuple[float, ...] | None = None,
        apply_rerank: bool | None = None,
        rerank_cfg: RerankConfig | None = None,
    ) -> str:
        import hashlib

        ctx_win = self._resolve_context_window(context_window)
        # ``apply_rerank`` is the per-call effective decision and
        # ``rerank_cfg`` the config snapshot taken alongside it (#1766) —
        # ``search()`` passes both so the key never re-reads attributes a
        # concurrent hot reload may have swapped. Direct callers omitting
        # them fall back to the live server state. A bypassed call
        # (rerank=False) is byte-identical to a server-off call with the
        # same args, so both deliberately share the "off" slot.
        if apply_rerank is None:
            apply_rerank = self.rerank_active
        if rerank_cfg is None:
            rerank_cfg = self._rerank_config
        if apply_rerank and rerank_cfg is not None:
            rerank_signal = (
                f"on:{rerank_cfg.oversample}:{rerank_cfg.min_pool}:{rerank_cfg.max_pool}"
            )
        else:
            rerank_signal = "off"
        # ADR-0011: scope + project_context_root MUST participate in the cache
        # key. Two callers from different projects must not share a cache slot
        # — the always-on context-boundary fragment differs.
        scope_signal = f"{scope}|{project_context_root}"
        cache_rrf_weights = (
            tuple(effective_rrf_weights)
            if effective_rrf_weights is not None
            else tuple(self._config.rrf_weights)
        )
        raw = (
            f"{query}|{top_k}|{source_filter}|{tag_filter}|{namespace}"
            f"|bm25={self._config.enable_bm25}:{self._config.bm25_candidates}"
            f"|dense={self._config.enable_dense}:{self._config.dense_candidates}"
            f"|rrf_k={self._config.rrf_k}|w={cache_rrf_weights}"
            f"|decay={self._decay_config.enabled}:{self._decay_config.half_life_days}"
            f"|mmr={self._mmr_config.enabled}:{self._mmr_config.lambda_param}"
            f"|ctx_win={ctx_win}"
            f"|rerank={rerank_signal}"
            f"|scope={scope_signal}"
            f"|metadata={metadata_filter}"
            f"|exclude_roots={tuple(str(root) for root in exclude_source_roots)}"
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

    async def _session_summary_boost_sources(
        self,
        query: str,
        scope_filter: ScopeFilter | None = None,
        project_context_root: Path | None = None,
    ) -> set[str]:
        """Stage-1 enrichment lookup against ``archive:session:*``.

        Runs a small BM25 lookup against the session-summary namespace
        (top-k = ``expansion_lookup_top_k``) and, for each hit above
        ``expansion_score_threshold``, follows ``chunk_links`` of type
        ``"summarizes"`` from the summary chunk back to the source
        chunks it summarized. The set of source files spanned by those
        chunks becomes the boost-sources list.

        ADR-0011 PR-D review: ``scope_filter`` / ``project_context_root``
        are threaded through so the rescue lookup honors the same
        always-on scope-context fragment as the primary retrieval.
        Without these, the new storage default ("no project context →
        user only") would silently exclude project_shared /
        project_local summaries from the rescue path even when the
        outer search is pinned to a project.

        Returns an empty set when the feature is disabled, the lookup
        fails, no summary scores above threshold, or no surviving
        ``summarizes`` link points at any source. The caller treats an
        empty set as "skip the rescue leg".
        """
        cfg = self._session_summary_config
        if cfg is None or cfg.expansion_lookup_top_k <= 0:
            return set()

        archive_filter = NamespaceFilter(pattern="archive:session:*")
        try:
            summary_hits = await self._storage.bm25_search(
                query,
                top_k=cfg.expansion_lookup_top_k,
                namespace_filter=archive_filter,
                scope_filter=scope_filter,
                project_context_root=project_context_root,
            )
        except Exception:
            _log_rescue_failure("summary_lookup", "session-summary lookup failed; skipping rescue")
            return set()

        threshold = cfg.expansion_score_threshold
        candidate_summaries = [r for r in summary_hits if r.score >= threshold]
        if not candidate_summaries:
            return set()

        # For each above-threshold summary, walk chunk_links(summarizes)
        # to reach the original source chunks, then collect their
        # source_file paths. Failures on any one summary are logged and
        # skipped — we never let a single bad summary mute the rescue.
        target_chunk_ids: list[UUID] = []
        for r in candidate_summaries:
            try:
                links = await self._storage.get_chunks_shared_from(
                    r.chunk.id, link_type="summarizes"
                )
            except Exception:
                _log_rescue_failure(
                    "links_walk", "get_chunks_shared_from failed for summary %s", r.chunk.id
                )
                continue
            for link in links:
                target_chunk_ids.append(link.target_id)

        if not target_chunk_ids:
            return set()

        try:
            chunks_map = await self._storage.get_chunks_batch(target_chunk_ids)
        except Exception:
            _log_rescue_failure("chunks_batch", "get_chunks_batch failed for rescue targets")
            return set()

        return {str(c.metadata.source_file) for c in chunks_map.values()}

    async def _rescue_retrieval(
        self,
        query: str,
        query_embedding: list[float],
        top_k: int,
        boost_sources: set[str],
        use_bm25: bool,
        use_dense: bool,
        scope_filter: ScopeFilter | None = None,
        project_context_root: Path | None = None,
        metadata_filter: SearchMetadataFilter | None = None,
        exhaustive: bool = False,
    ) -> list[SearchResult]:
        """Parallel BM25+dense retrieval restricted to boost_sources.

        Runs unrestricted retrievals and post-filters by source_file
        membership rather than threading a new parameter through the
        storage primitives — keeps ``bm25_search`` / ``dense_search``
        signatures clean. The leg is merged into RRF as a third input
        list, weighted by ``expansion_rescue_weight``.

        ADR-0011 PR-D review: scope context is threaded through so the
        rescue legs honor the same always-on scope filter the primary
        retrieval uses. The default ("no project context → user only")
        would otherwise drop project_shared / project_local rescue
        candidates whenever the outer search was pinned to a project.
        """
        if not boost_sources:
            return []
        # Cast a slightly wider net so source filtering still leaves a
        # useful candidate pool. Bounded by existing candidate caps.
        oversample = max(top_k, self._config.bm25_candidates)
        metadata_kwargs = (
            {"metadata_filter": metadata_filter} if metadata_filter is not None else {}
        )

        async def _bm25_leg() -> list[SearchResult]:
            if not use_bm25:
                return []
            try:
                hits = await self._storage.bm25_search(
                    query,
                    top_k=oversample,
                    namespace_filter=None,
                    scope_filter=scope_filter,
                    project_context_root=project_context_root,
                    **metadata_kwargs,
                )
            except Exception:
                _log_rescue_failure("bm25_leg", "rescue bm25 leg failed")
                return []
            return [r for r in hits if str(r.chunk.metadata.source_file) in boost_sources]

        async def _dense_leg() -> list[SearchResult]:
            if not use_dense or not query_embedding:
                return []
            try:
                hits = await self._storage.dense_search(
                    query_embedding,
                    top_k=oversample,
                    namespace_filter=None,
                    scope_filter=scope_filter,
                    project_context_root=project_context_root,
                    exhaustive=exhaustive,
                    **metadata_kwargs,
                )
            except Exception:
                _log_rescue_failure("dense_leg", "rescue dense leg failed")
                return []
            return [r for r in hits if str(r.chunk.metadata.source_file) in boost_sources]

        bm25_rescue, dense_rescue = await asyncio.gather(_bm25_leg(), _dense_leg())

        # Merge the rescue leg's BM25 + dense candidates into a single
        # ranked list (keep best rank across the two legs) so we feed
        # one rescue list into the outer RRF rather than two — this
        # matches the RFC's "third input list" framing and avoids
        # over-weighting rescue when it dominates both legs.
        seen: dict[UUID, SearchResult] = {}
        for r in bm25_rescue + dense_rescue:
            existing = seen.get(r.chunk.id)
            if existing is None or r.score > existing.score:
                seen[r.chunk.id] = r
        return sorted(seen.values(), key=lambda r: r.score, reverse=True)[:oversample]

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

    async def _filter_only_search(
        self,
        *,
        top_k: int | None,
        source_filter: str | None,
        tag_filter: str | None,
        namespace: str | list[str] | None,
        context_window: int | None,
        as_of_unix: int | None,
        scope_filter: ScopeFilter | None = None,
        project_context_root: Path | None = None,
        metadata_filter: SearchMetadataFilter | None = None,
        exclude_source_roots: tuple[Path, ...] = (),
        record: bool = True,
    ) -> tuple[list[SearchResult], RetrievalStats]:
        """Empty-query path (#750): enumerate by filter, skip retrievers.

        Tag and/or source filter become the primary selectors via
        ``recall_chunks``. Each candidate enters with ``score=1.0`` so
        the post-filter stages (validity → decay → access → importance)
        produce a meaningful order even without a keyword to rank by.
        MMR and the reranker are skipped — both need a query/embedding
        signal that doesn't exist in this mode.
        """
        import time

        top_k = self._config.default_top_k if top_k is None else top_k
        ns_filter = NamespaceFilter.parse(
            namespace,
            system_prefixes=tuple(self._config.system_namespace_prefixes),
        )

        # Over-sample so the validity stage can prune without starving
        # the response. ``top_k * 5`` mirrors the rerank-pool floor logic
        # used in the keyword path; floored at 100 for tiny ``top_k``.
        # Worst case ≈ 2500 candidates given the route's ``top_k <= 500``
        # cap, which is the same order as ``bm25_candidates``.
        #
        # On a popular tag (chunk count > candidate_limit) older highly
        # accessed/important chunks beyond this recency cutoff never reach
        # the boost stages — ``recall_chunks`` orders ``created_at DESC``,
        # so this is a "browse newest by tag" UX, not "rank everything by
        # importance". The keyword path doesn't have this floor because
        # BM25/dense pre-filter on relevance; here recency is the only
        # signal we have to cap on. Acceptable for the click-the-pill
        # workflow; revisit if a "show me old-but-important by tag" need
        # surfaces.
        #
        # Exclusions are applied after this bounded enumeration. A large set
        # of newer excluded sources can therefore consume the candidate
        # budget and leave fewer than ``top_k`` eligible results. Increasing
        # the storage query limit dynamically would require source-root
        # filtering in storage; keep this bounded behavior explicit for now.
        candidate_limit = max(top_k * 5, 100)
        chunks = await self._storage.recall_chunks(
            source_filter=source_filter,
            tag_filter=tag_filter,
            namespace_filter=ns_filter,
            scope_filter=scope_filter,
            project_context_root=project_context_root,
            limit=candidate_limit,
            metadata_filter=metadata_filter,
        )

        fused: list[SearchResult] = [
            SearchResult(chunk=c, score=1.0, rank=i + 1, source="recall")
            for i, c in enumerate(chunks)
        ]
        fused = _exclude_source_roots(fused, exclude_source_roots)
        # score_scale="none": there is no relevance scale — the filter is
        # the selector. Scores start at 1.0 and only the decay/access/
        # importance modifiers (when enabled) differentiate them, so there
        # is no relevance magnitude to gate on.
        stats = RetrievalStats(fused_total=len(fused), score_scale="none")

        effective_as_of = as_of_unix if as_of_unix is not None else int(time.time())
        if fused:
            fused = _apply_validity_filter(fused, effective_as_of)

        if self._decay_config.enabled and fused:
            from memtomem.search.decay import apply_score_decay

            # Pin decay to the validity instant so replay is time-stable (#1802).
            fused = apply_score_decay(
                fused,
                half_life_days=self._decay_config.half_life_days,
                now=datetime.fromtimestamp(effective_as_of, tz=UTC),
            )

        if self._access_config.enabled and fused:
            from memtomem.search.access import apply_access_boost

            access_chunk_ids = [r.chunk.id for r in fused]
            access_counts = await self._storage.get_access_counts(access_chunk_ids)
            fused = apply_access_boost(
                fused, access_counts, max_boost=self._access_config.max_boost
            )

        if self._importance_config and getattr(self._importance_config, "enabled", False) and fused:
            from memtomem.search.importance import apply_importance_boost

            chunk_ids_imp = [r.chunk.id for r in fused]
            imp_scores = await self._storage.get_importance_scores(chunk_ids_imp)
            fused = apply_importance_boost(
                fused,
                imp_scores,
                max_boost=getattr(self._importance_config, "max_boost", 1.5),
            )

        # Re-sort after boosts and trim to ``top_k``. Boost stages mutate
        # scores but preserve list order; the keyword path relies on the
        # reranker / RRF for final ordering, so this branch sorts here.
        fused.sort(key=lambda r: r.score, reverse=True)
        fused = fused[:top_k]

        ctx_win = self._resolve_context_window(context_window)
        if ctx_win > 0 and fused:
            fused = await self._expand_context(fused, ctx_win)
            fused = _exclude_source_roots(fused, exclude_source_roots)

        stats.final_total = len(fused)

        # Skipped under ``record=False`` (#1802) — replay mutates no counters.
        if fused and record:

            async def _increment():
                await self._storage.increment_access([r.chunk.id for r in fused])

            t = asyncio.create_task(_increment())
            t.add_done_callback(_bg_task_error_cb)
            self._bg_tasks.add(t)
            t.add_done_callback(self._bg_tasks.discard)

        return fused, stats

    async def search(
        self,
        query: str,
        top_k: int | None = None,
        source_filter: str | None = None,
        tag_filter: str | None = None,
        namespace: str | list[str] | None = None,
        rrf_weights: list[float] | None = None,
        context_window: int | None = None,
        as_of_unix: int | None = None,
        scope: str | list[str] | None = None,
        project_context_root: Path | None = None,
        source_exact: list[str] | tuple[str, ...] | None = None,
        chunk_types: list[str] | tuple[str, ...] | None = None,
        created_from: datetime | None = None,
        created_before: datetime | None = None,
        exclude_source_roots: tuple[Path, ...] | None = None,
        rerank: bool | None = None,
        origin: str = "internal",
        record: bool = True,
    ) -> tuple[list[SearchResult], RetrievalStats]:
        # ``record`` (#1802): the default (True) is today's behavior. False is
        # the no-side-effects replay/evaluation mode — the call touches no
        # persistent or cross-call state: it neither reads nor writes the TTL
        # result cache or the LLM-expansion cache, does not increment access
        # counters, and does not persist a query-run observation (so
        # ``stats.query_run_id`` stays None). It also switches the dense legs
        # to exhaustive KNN so equal-distance boundary rows are selected
        # deterministically (see ``dense_search(exhaustive=...)``). Pair it
        # with an explicit ``as_of_unix`` to pin validity + decay to a fixed
        # instant for byte-reproducible replays.
        #
        # ``rerank`` (#1766): None = follow server config; False = skip the
        # Stage 3b cross-encoder and collapse the candidate pool to top_k
        # (per-call fast path for latency-bounded callers); True = follow
        # config — it cannot force-enable when no reranker is wired.
        # #750: tag/source-only branch — no keyword to rank by, so the
        # filter takes over as the primary selector. We enumerate via
        # ``recall_chunks`` and skip BM25/dense/expansion/rescue/rerank
        # entirely; post-filter stages (validity, decay, access,
        # importance, ctx-window) still apply so ranking reflects
        # recency × access × importance.
        scope_filter = ScopeFilter.parse(scope)
        metadata_candidate = SearchMetadataFilter(
            source_exact=tuple(sorted(set(source_exact or ()))),
            chunk_types=tuple(sorted(set(chunk_types or ()))),
            created_from=created_from,
            created_before=created_before,
        )
        metadata_filter: SearchMetadataFilter | None = metadata_candidate
        normalized_exclusion_roots = _normalize_source_roots(exclude_source_roots)
        if not any(
            (
                metadata_candidate.source_exact,
                metadata_candidate.chunk_types,
                metadata_candidate.created_from,
                metadata_candidate.created_before,
            )
        ):
            metadata_filter = None
        query = (query or "").strip()
        if not query:
            if not (tag_filter or source_filter or metadata_filter):
                return [], RetrievalStats()
            return await self._filter_only_search(
                top_k=top_k,
                source_filter=source_filter,
                tag_filter=tag_filter,
                namespace=namespace,
                context_window=context_window,
                as_of_unix=as_of_unix,
                scope_filter=scope_filter,
                project_context_root=project_context_root,
                metadata_filter=metadata_filter,
                exclude_source_roots=normalized_exclusion_roots,
                record=record,
            )

        original_query = query
        top_k = self._config.default_top_k if top_k is None else top_k
        effective_weights = rrf_weights or self._config.rrf_weights
        started_at = time.perf_counter()

        # Lease the reranker generation for the whole ranked-search body
        # (#1777): hot reload retires (and eventually closes) these while a
        # search may be awaiting, so every rerank-dependent site below
        # (cache key, pool widening, Stage 3b) reads this one snapshot, and
        # the instance cannot be closed out from under the call.
        with self._lease_reranker() as (reranker, rerank_cfg):
            apply_rerank = reranker is not None and rerank_cfg is not None and rerank is not False

            # Check TTL cache for identical queries
            # ``as_of_unix`` is intentionally excluded from ``cache_key`` and
            # bypasses the cache entirely when explicit. Default-path (None)
            # callers fall through to ``int(time.time())`` below and reuse
            # cached results within ``cache_ttl`` — accepting up-to-TTL
            # staleness near a date boundary, which the RFC's date-only
            # bounds already absorb.
            cache_key = self._cache_key(
                query,
                top_k,
                source_filter,
                tag_filter,
                namespace,
                context_window,
                scope=scope,
                project_context_root=project_context_root,
                metadata_filter=metadata_filter,
                exclude_source_roots=normalized_exclusion_roots,
                effective_rrf_weights=effective_weights,
                apply_rerank=apply_rerank,
                rerank_cfg=rerank_cfg,
            )
            version_at_start = self._cache_version
            ttl_snapshot = self._cache_ttl
            # ``record=False`` (replay) bypasses the TTL result cache in both
            # directions: it must never be served a cached result nor evict
            # one, so a concurrent interactive search's cache is untouched.
            if record and as_of_unix is None and cache_key in self._search_cache:
                ts, ver, cached_results, cached_stats = self._search_cache[cache_key]
                if ver == self._cache_version and time.time() - ts < ttl_snapshot:
                    run_stats = dataclass_replace(
                        cached_stats,
                        query_run_id=None,
                        cache_hit=True,
                        latency_ms=round((time.perf_counter() - started_at) * 1000, 3),
                    )
                    run_stats.query_run_id = await self._record_ranked_search(
                        query=original_query,
                        query_embedding=[],
                        results=cached_results,
                        stats=run_stats,
                        top_k=top_k,
                        origin=origin,
                        namespace=namespace,
                        scope=scope,
                        source_filter=source_filter,
                        tag_filter=tag_filter,
                        metadata_filter=metadata_filter,
                        as_of_unix=as_of_unix,
                        rrf_weights=effective_weights,
                    )
                    return cached_results, run_stats
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
            # Never compare a query vector generated under the current policy
            # with stored vectors from another policy/model/dimension. BM25
            # remains available as the safe degraded path.
            mismatch = getattr(self._storage, "embedding_mismatch", None)
            use_dense = self._config.enable_dense and not isinstance(mismatch, dict)
            metadata_kwargs = (
                {"metadata_filter": metadata_filter} if metadata_filter is not None else {}
            )

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
                    # ADR-0011 PR-D round 11: thread the outer search's
                    # project context onto the heading-expansion's dense
                    # probe so it samples from the same scope set the
                    # primary retrieval is pinned to. ``exhaustive`` carries
                    # replay's deterministic-dense mode into this leg too.
                    query = await expand_query_headings(
                        query,
                        self._storage,
                        self._embedder,
                        max_terms,
                        project_context_root=project_context_root,
                        exhaustive=not record,
                    )
                if strategy == "llm":
                    # Replay (``record=False``) neither reads nor writes the
                    # expansion cache, so it cannot depend on nor mutate hidden
                    # prior pipeline state.
                    cached_expansion = self._expansion_cache.get(query) if record else None
                    if cached_expansion is not None:
                        query = cached_expansion
                    elif self._llm_provider is not None:
                        try:
                            original = query
                            query = await expand_query_llm(
                                query,
                                self._llm_provider,
                                max_terms,  # type: ignore[arg-type]
                            )
                            if record:
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
                    self._storage.bm25_search(
                        query,
                        top_k=bm25_k,
                        namespace_filter=ns_filter,
                        scope_filter=scope_filter,
                        project_context_root=project_context_root,
                        **metadata_kwargs,
                    )
                )
            dense_error: str | None = None
            if use_dense:
                try:
                    query_embedding = await self._embedder.embed_query(query)
                    dense_results = await self._storage.dense_search(
                        query_embedding,
                        top_k=dense_k,
                        namespace_filter=ns_filter,
                        scope_filter=scope_filter,
                        project_context_root=project_context_root,
                        exhaustive=not record,
                        **metadata_kwargs,
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

            bm25_results = _exclude_source_roots(bm25_results, normalized_exclusion_roots)
            dense_results = _exclude_source_roots(dense_results, normalized_exclusion_roots)

            # Candidate counts describe the eligible inputs entering fusion, not
            # the raw retriever hit counts. This keeps compose telemetry aligned
            # with the pool that can actually surface after source exclusion.
            stats = RetrievalStats(
                bm25_candidates=len(bm25_results),
                dense_candidates=len(dense_results),
                bm25_error=bm25_error,
                dense_error=dense_error,
                hidden_system_ns=hidden_system_ns,
                rerank_applied=apply_rerank,
            )

            # Stage 1 enrichment (RFC P1 Phase C): session-summary rescue.
            # When an above-threshold past-session summary's chunk_links
            # point at source files relevant to this query, run a parallel
            # BM25+dense retrieval restricted to those files and merge the
            # result as a third RRF input. This brings past-session chunks
            # into ranking contention without changing the retrieval
            # primitives' signatures — keeping ``bm25_search`` /
            # ``dense_search`` archive-agnostic. (Pure post-fusion score
            # multiplier was rejected: it can only re-rank candidates that
            # already surfaced organically; "ranking contention" requires
            # injection.)
            rescue_results: list[SearchResult] = []
            rescue_chunk_ids: set[UUID] = set()
            if (
                namespace is None
                and self._session_summary_config is not None
                and self._session_summary_config.expansion_lookup_top_k > 0
            ):
                try:
                    boost_sources = await self._session_summary_boost_sources(
                        query,
                        scope_filter=scope_filter,
                        project_context_root=project_context_root,
                    )
                    if boost_sources:
                        rescue_results = await self._rescue_retrieval(
                            query,
                            query_embedding,
                            top_k=top_k,
                            boost_sources=boost_sources,
                            use_bm25=use_bm25,
                            use_dense=use_dense,
                            scope_filter=scope_filter,
                            project_context_root=project_context_root,
                            metadata_filter=metadata_filter,
                            exhaustive=not record,
                        )
                        rescue_chunk_ids = {r.chunk.id for r in rescue_results}
                except Exception:
                    _log_rescue_failure("rescue_wiring", "session-summary rescue leg failed")

            # Stage 3: fusion (or single-retriever passthrough)
            # When reranking is active, widen the candidate pool so the
            # cross-encoder can rescue items RRF ranked just outside top_k.
            # pool = clamp(oversample * top_k, [min_pool, max_pool]) — scales
            # with the request and bounded by cost controls. Collapses to
            # top_k when reranking is disabled or bypassed per-call
            # (rerank=False, #1766) so single-retriever passthrough size is
            # unchanged — the widening exists only to feed the reranker.
            if apply_rerank and rerank_cfg is not None:
                rerank_pool = max(
                    rerank_cfg.min_pool,
                    min(rerank_cfg.max_pool, int(rerank_cfg.oversample * top_k)),
                )
            else:
                rerank_pool = top_k

            # Mark rescue-leg results so fusion can OR-propagate the flag.
            if rescue_results:
                rescue_results = _exclude_source_roots(rescue_results, normalized_exclusion_roots)
                rescue_results = [
                    dataclass_replace(r, via_session_summary=True) for r in rescue_results
                ]

            # Single source for the rescue leg's RRF weight — the fallback is
            # effectively unreachable (rescue only fires when the config is
            # wired) but keeps the three fusion branches total.
            rescue_w = (
                self._session_summary_config.expansion_rescue_weight
                if self._session_summary_config is not None
                else _DEFAULT_RESCUE_WEIGHT
            )

            if use_bm25 and use_dense:
                fusion_lists = [bm25_results, dense_results]
                fusion_weights = list(effective_weights)
                fusion_labels = ["bm25", "dense"]
                if rescue_results:
                    fusion_lists.append(rescue_results)
                    fusion_weights.append(rescue_w)
                    fusion_labels.append("session_rescue")
                fused = reciprocal_rank_fusion(
                    fusion_lists,
                    k=self._config.rrf_k,
                    top_k=rerank_pool,
                    weights=fusion_weights,
                    list_labels=fusion_labels,
                )
                stats.score_scale = "rrf"
            elif use_bm25:
                if rescue_results:
                    fused = reciprocal_rank_fusion(
                        [bm25_results, rescue_results],
                        k=self._config.rrf_k,
                        top_k=rerank_pool,
                        weights=[effective_weights[0], rescue_w],
                        list_labels=["bm25", "session_rescue"],
                    )
                    stats.score_scale = "rrf"
                else:
                    # Passthrough keeps raw retriever scores — a distinct
                    # scale (#1767): Okapi BM25 is unbounded positive, far
                    # outside the RRF ceiling.
                    fused = bm25_results[:rerank_pool]
                    stats.score_scale = "bm25"
            elif use_dense:
                if rescue_results:
                    fused = reciprocal_rank_fusion(
                        [dense_results, rescue_results],
                        k=self._config.rrf_k,
                        top_k=rerank_pool,
                        weights=[
                            effective_weights[1] if len(effective_weights) > 1 else 1.0,
                            rescue_w,
                        ],
                        list_labels=["dense", "session_rescue"],
                    )
                    stats.score_scale = "rrf"
                else:
                    fused = dense_results[:rerank_pool]
                    stats.score_scale = "dense"
            else:
                fused = []
            stats.fused_total = len(fused)

            # Stage 3b: Cross-encoder reranking (skipped when bypassed per-call
            # via rerank=False, #1766). ``apply_rerank`` already implies the
            # reranker pair is non-None; restating both here is for type
            # narrowing, mirroring the cache-key and pool-widening sites.
            if apply_rerank and reranker is not None and rerank_cfg is not None and fused:
                try:
                    fused = await reranker.rerank(query, fused, top_k=top_k)
                except Exception as exc:
                    logger.warning("Reranking failed, using original order: %s", exc)
                    # Fallback must still honor the caller's response size —
                    # fused is at rerank_pool (e.g. 20) right now, not top_k.
                    fused = fused[:top_k]
                else:
                    # Providers also fall back silently (returning the input
                    # unchanged) on their own errors, so the decision flag
                    # alone can't label the scale — only a list the reranker
                    # actually re-scored (every item re-stamped "reranked")
                    # switches it (#1767).
                    if fused and all(r.source == "reranked" for r in fused):
                        stats.score_scale = "rerank"
                        stats.reranker_model = rerank_cfg.model

            # Filter by source file if requested
            if source_filter:
                fused = [
                    r
                    for r in fused
                    if match_source_filter(source_filter, str(r.chunk.metadata.source_file))
                ]

            # Filter by tag if requested (comma-separated = OR matching)
            if tag_filter:
                required = {t.strip() for t in tag_filter.split(",") if t.strip()}
                fused = [r for r in fused if required & set(r.chunk.metadata.tags)]

            if metadata_filter is not None:
                fused = [r for r in fused if _matches_metadata(r, metadata_filter)]

            # Stage β': temporal-validity filter (RFC §Pipeline integration).
            # AND-combined with source/tag filter via sequential application —
            # a chunk must pass both to survive. Default ``as_of`` is the
            # current wall-clock; explicit values bypass the result cache so
            # historical queries don't poison default-path cache slots.
            effective_as_of = as_of_unix if as_of_unix is not None else int(time.time())
            if fused:
                fused = _apply_validity_filter(fused, effective_as_of)

            # Stage 4: Time decay (re-score older chunks lower)
            if self._decay_config.enabled and fused:
                from memtomem.search.decay import apply_score_decay

                # Pin decay to the same instant as validity (#1802): otherwise
                # ``apply_score_decay`` defaults to wall clock and a replay with
                # an explicit ``as_of_unix`` would still drift with real time.
                fused = apply_score_decay(
                    fused,
                    half_life_days=self._decay_config.half_life_days,
                    now=datetime.fromtimestamp(effective_as_of, tz=UTC),
                )

            # Stage 5: MMR diversity re-ranking
            if self._mmr_config.enabled and not use_dense:
                # #1619: without dense retrieval there are no vectors to
                # diversify over, so an explicitly enabled MMR silently did
                # nothing. Say so once per process (INFO — it's a config
                # mismatch, not a runtime failure); mem_status carries the
                # same fact as a persistent `mmr_disabled_no_dense` warning.
                _log_mmr_no_dense_once()
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
            if (
                self._importance_config
                and getattr(self._importance_config, "enabled", False)
                and fused
            ):
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
                fused = _exclude_source_roots(fused, normalized_exclusion_roots)
                if metadata_filter is not None and metadata_filter.source_exact:
                    # Context neighbors belong to the selected source but may have
                    # a different chunk type or timestamp. Keep that surrounding
                    # context while preserving the exact-source boundary.
                    allowed_sources = {
                        norm_path(Path(value)) for value in metadata_filter.source_exact
                    }
                    fused = [
                        r
                        for r in fused
                        if norm_path(r.chunk.metadata.source_file) in allowed_sources
                    ]

            # Re-stamp ``via_session_summary`` for any chunk that came in
            # via the rescue leg. Downstream stages (decay, MMR, access,
            # importance, reranker, context expansion) construct fresh
            # ``SearchResult`` instances with default field values and so
            # silently drop the flag — restoring here at the boundary keeps
            # propagation a single-source-of-truth concern of the pipeline
            # rather than leaking the obligation into every stage.
            if rescue_chunk_ids:
                fused = [
                    dataclass_replace(r, via_session_summary=True)
                    if r.chunk.id in rescue_chunk_ids
                    else r
                    for r in fused
                ]

            stats.final_total = len(fused)

            # Increment access counts for returned results (fire-and-forget).
            # Skipped under ``record=False`` (#1802): replay must not mutate the
            # counters that feed the access/importance boosts, or a later
            # baseline-vs-candidate comparison would drift.
            if fused and record:

                async def _increment():
                    await self._storage.increment_access([r.chunk.id for r in fused])

                t = asyncio.create_task(_increment())
                t.add_done_callback(_bg_task_error_cb)
                self._bg_tasks.add(t)
                t.add_done_callback(self._bg_tasks.discard)

            stats.latency_ms = round((time.perf_counter() - started_at) * 1000, 3)
            # Replay persists no observation, so no run id is minted.
            stats.query_run_id = (
                await self._record_ranked_search(
                    query=original_query,
                    query_embedding=query_embedding if use_dense else [],
                    results=fused,
                    stats=stats,
                    top_k=top_k,
                    origin=origin,
                    namespace=namespace,
                    scope=scope,
                    source_filter=source_filter,
                    tag_filter=tag_filter,
                    metadata_filter=metadata_filter,
                    as_of_unix=as_of_unix,
                    rrf_weights=effective_weights,
                )
                if record
                else None
            )

            # Store in TTL cache only if version hasn't changed during search.
            # Skip the write when ``as_of_unix`` was explicit so that a
            # historical-query result never overwrites the default-path slot
            # (next default caller would otherwise be served a past-snapshot
            # filtering of the same query).
            if record and as_of_unix is None and self._cache_version == version_at_start:
                cache_stats = dataclass_replace(
                    stats, query_run_id=None, cache_hit=False, latency_ms=None
                )
                self._search_cache[cache_key] = (
                    time.time(),
                    version_at_start,
                    fused,
                    cache_stats,
                )
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
        # Retired-but-still-leased reranker generations first (#1777):
        # emptying the set now means a lease released after this point finds
        # no retired entry and schedules nothing — no orphan task, no double
        # close. Already-scheduled deferred closes sit in _bg_tasks below.
        while self._retired_rerank_entries:
            entry = self._retired_rerank_entries.pop()
            await close_reranker_safely(entry.reranker)
        if self._bg_tasks:
            await asyncio.gather(*self._bg_tasks, return_exceptions=True)
            self._bg_tasks.clear()
        if self._reranker is not None and hasattr(self._reranker, "close"):
            await self._reranker.close()
