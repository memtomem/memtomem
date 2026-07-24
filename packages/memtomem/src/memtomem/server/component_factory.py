"""Shared initialisation factory for MCP server."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import asyncio
import inspect
import logging

from memtomem.chunking.base import Chunker
from memtomem.chunking.markdown import MarkdownChunker
from memtomem.chunking.registry import ChunkerRegistry
from memtomem.chunking.restructured_text import ReStructuredTextChunker
from memtomem.chunking.structured import StructuredChunker
from memtomem.config import Mem2MemConfig, embedding_policy_fingerprint
from memtomem.embedding.factory import create_embedder
from memtomem.errors import EmbeddingDimensionMismatchError
from memtomem.indexing.engine import IndexEngine
from memtomem.search.pipeline import SearchPipeline
from memtomem.storage.factory import create_storage
from memtomem.storage.sqlite_backend import SqliteBackend

if TYPE_CHECKING:
    from memtomem.embedding.base import EmbeddingProvider
    from memtomem.llm.base import LLMProvider

_log = logging.getLogger(__name__)


async def _close_resource(
    resource: object | None, label: str
) -> tuple[bool, asyncio.CancelledError | None]:
    """Best-effort close used by both startup rollback and normal shutdown.

    Returns ``(closed, cancelled)``: ``closed`` is ``True`` only when the
    close completed without error — callers that gate follow-up work on a
    *confirmed* close (the instance-registry release must not advertise a
    closed store while the sqlite handle may still be open, #1935) branch
    on it. ``cancelled`` carries a :class:`asyncio.CancelledError` caught
    mid-close so teardown orchestrators can defer and re-raise it after
    settlement instead of silently swallowing it (the pre-#1935 behavior
    of the bare ``except BaseException``).
    """
    if resource is None:
        return True, None
    close = getattr(resource, "close", None)
    if not callable(close):
        return True, None
    try:
        result = close()
        if inspect.isawaitable(result):
            await result
        return True, None
    except asyncio.CancelledError as exc:
        _log.warning("Cancelled while closing %s", label)
        return False, exc
    except BaseException:
        _log.warning("Failed to close %s", label, exc_info=True)
        return False, None


@dataclass(frozen=True)
class TeardownResult:
    """Outcome of :func:`close_components`.

    ``storage_closed`` is the load-bearing bit: only a confirmed storage
    close permits releasing the instance-registry sentinel (a failed or
    cancelled storage close retains it — a possibly-open store must stay
    advertised). ``cancelled`` is the first cancellation caught across
    the close sequence, deferred to the caller.
    """

    storage_closed: bool
    cancelled: asyncio.CancelledError | None = None


@dataclass
class Components:
    """Container for initialised core components."""

    config: Mem2MemConfig
    storage: SqliteBackend
    embedder: EmbeddingProvider
    index_engine: IndexEngine
    search_pipeline: SearchPipeline
    llm: LLMProvider | None = None
    # Populated when startup detected a ``chunks_vec`` / provider mismatch
    # (``EmbeddingDimensionMismatchError``) and the server came up in
    # degraded mode instead of crashing. The dict has the same shape as
    # ``SqliteBackend.embedding_mismatch``. See issue #349.
    embedding_broken: dict | None = None


async def create_components(
    config: Mem2MemConfig | None = None,
    *,
    load_ambient_config: bool = True,
) -> Components:
    """Create and initialise all core components.

    ``load_ambient_config=False`` is reserved for callers that have already
    resolved the complete configuration precedence chain.  The default keeps
    the existing server and CLI behaviour of loading ``config.d``, persisted
    overrides, and environment variables before constructing components.
    """
    from memtomem.config import load_config_d, load_config_overrides

    config = config or Mem2MemConfig()
    if load_ambient_config:
        load_config_d(config)
        load_config_overrides(config)

    # Initialize FTS tokenizer from config
    from memtomem.storage.fts_tokenizer import set_tokenizer

    if config.search.tokenizer != "unicode61":
        set_tokenizer(config.search.tokenizer)

    storage = create_storage(config)
    embedder: EmbeddingProvider | None = None
    embedding_broken: dict | None = None
    reranker: object | None = None
    llm: LLMProvider | None = None
    search_pipeline: SearchPipeline | None = None
    try:
        embedder = create_embedder(config.embedding)
        await storage.initialize()
    except EmbeddingDimensionMismatchError:
        # Stored DB has ``embedding_dimension=0`` (prior NoopEmbedder / BM25
        # install) but the runtime config points at a real provider. Instead
        # of crashing the server — which leaves the user no MCP-level path to
        # repair — come up in degraded mode: the storage is re-opened with
        # ``strict_dim_check=False`` (same seam the ``mm embedding-reset``
        # CLI uses) so the mismatch surfaces as a structured flag and the
        # recovery tool (``mem_embedding_reset``) stays callable over MCP.
        # Vector-dependent tools (``mem_add`` / ``mem_index`` / …) are gated
        # separately via ``_check_embedding_mismatch``. See issue #349.
        await storage.close()
        _log.warning(
            "Embedding dimension mismatch detected at startup — entering "
            "degraded mode. Non-vector tools (mem_status, mem_stats, "
            "mem_embedding_reset, mem_list, mem_read) stay available; "
            "vector-dependent tools (mem_add, mem_index, ...) will return "
            "an actionable error until `mem_embedding_reset` is run."
        )
        storage = SqliteBackend(
            config.storage,
            dimension=config.embedding.dimension,
            embedding_provider=config.embedding.provider,
            embedding_model=config.embedding.model,
            embedding_policy_fingerprint=embedding_policy_fingerprint(config.embedding),
            embedding_max_sequence_tokens=config.embedding.max_sequence_tokens,
            strict_dim_check=False,
        )
        try:
            await storage.initialize()
        except Exception:
            if embedder is not None:
                await embedder.close()
            await storage.close()
            raise
        embedding_broken = storage.embedding_mismatch
    except Exception:
        if embedder is not None:
            await embedder.close()
        await storage.close()
        raise
    assert embedder is not None

    # Model/policy mismatches are non-fatal at schema initialization, but use
    # the same degraded-mode signal as dimension mismatches so watchers and
    # startup soft-sync cannot write mixed-vector data.
    if embedding_broken is None:
        embedding_broken = storage.embedding_mismatch

    try:
        # Build chunker registry with optional code chunkers
        chunkers: list[Chunker] = [
            MarkdownChunker(indexing_config=config.indexing),
            StructuredChunker(indexing_config=config.indexing),
            ReStructuredTextChunker(),
        ]
        try:
            from memtomem.chunking.python_code import PythonChunker

            chunkers.append(PythonChunker())
        except Exception:
            _log.warning(
                "PythonChunker unavailable — install memtomem[all] to enable tree-sitter code chunking",
                exc_info=True,
            )
        try:
            from memtomem.chunking.javascript import JavaScriptChunker

            chunkers.append(JavaScriptChunker())
        except Exception:
            _log.warning(
                "JavaScriptChunker unavailable — install memtomem[all] to enable tree-sitter code chunking",
                exc_info=True,
            )
        registry = ChunkerRegistry(chunkers)

        if config.rerank.enabled:
            from memtomem.search.reranker.factory import create_reranker

            reranker = create_reranker(config.rerank)

        # One shared LLM client serves indexing and search.
        if config.llm.enabled:
            from memtomem.llm.factory import create_llm

            llm = create_llm(config.llm)

        index_engine = IndexEngine(
            storage=storage,
            embedder=embedder,
            config=config.indexing,
            registry=registry,
            namespace_config=config.namespace,
            progress_threshold=config.embedding.progress_threshold,
            llm=llm,
        )

        search_pipeline = SearchPipeline(
            storage=storage,
            embedder=embedder,
            config=config.search,
            decay_config=config.decay,
            mmr_config=config.mmr,
            access_config=config.access,
            reranker=reranker,
            rerank_config=config.rerank,
            expansion_config=config.query_expansion,
            importance_config=config.importance,
            context_window_config=config.context_window,
            llm_provider=llm,
            session_summary_config=config.session_summary,
        )

        return Components(
            config=config,
            storage=storage,
            embedder=embedder,
            index_engine=index_engine,
            search_pipeline=search_pipeline,
            llm=llm,
            embedding_broken=embedding_broken,
        )
    except BaseException:
        # Once SearchPipeline exists it owns the reranker. Before that point
        # the factory owns and closes a standalone reranker itself.
        await _close_resource(search_pipeline, "search pipeline")
        if search_pipeline is None:
            await _close_resource(reranker, "reranker")
        await _close_resource(llm, "LLM provider")
        await _close_resource(embedder, "embedder")
        await _close_resource(storage, "storage")
        raise


async def close_components(comp: Components) -> TeardownResult:
    """Shut down every component even when an earlier close fails."""
    first_cancel: asyncio.CancelledError | None = None
    for resource, label in (
        (comp.search_pipeline, "search pipeline"),
        (comp.llm, "LLM provider"),
        (comp.embedder, "embedder"),
    ):
        _, cancelled = await _close_resource(resource, label)
        if first_cancel is None:
            first_cancel = cancelled
    storage_closed, cancelled = await _close_resource(comp.storage, "storage")
    if first_cancel is None:
        first_cancel = cancelled
    return TeardownResult(storage_closed=storage_closed, cancelled=first_cancel)
