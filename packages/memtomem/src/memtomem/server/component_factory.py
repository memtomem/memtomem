"""Shared initialisation factory for MCP server."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import logging

from memtomem.chunking.base import Chunker
from memtomem.chunking.markdown import MarkdownChunker
from memtomem.chunking.registry import ChunkerRegistry
from memtomem.chunking.restructured_text import ReStructuredTextChunker
from memtomem.chunking.structured import StructuredChunker
from memtomem.config import Mem2MemConfig
from memtomem.embedding.factory import create_embedder
from memtomem.indexing.engine import IndexEngine
from memtomem.search.pipeline import SearchPipeline
from memtomem.storage.factory import create_storage
from memtomem.storage.sqlite_backend import SqliteBackend

if TYPE_CHECKING:
    from memtomem.embedding.base import EmbeddingProvider
    from memtomem.llm.base import LLMProvider

_log = logging.getLogger(__name__)


@dataclass
class Components:
    """Container for initialised core components."""

    config: Mem2MemConfig
    storage: SqliteBackend
    embedder: EmbeddingProvider
    index_engine: IndexEngine
    search_pipeline: SearchPipeline
    llm: LLMProvider | None = None


async def create_components(config: Mem2MemConfig | None = None) -> Components:
    """Create and initialise all core components."""
    from memtomem.config import load_config_d, load_config_overrides

    config = config or Mem2MemConfig()
    load_config_d(config)
    load_config_overrides(config)

    # Initialize FTS tokenizer from config
    from memtomem.storage.fts_tokenizer import set_tokenizer

    if config.search.tokenizer != "unicode61":
        set_tokenizer(config.search.tokenizer)

    storage = create_storage(config)
    embedder: EmbeddingProvider | None = None
    try:
        embedder = create_embedder(config.embedding)
        await storage.initialize()
    except Exception:
        if embedder is not None:
            await embedder.close()
        await storage.close()
        raise
    assert embedder is not None

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

    index_engine = IndexEngine(
        storage=storage,
        embedder=embedder,
        config=config.indexing,
        registry=registry,
        namespace_config=config.namespace,
    )
    # Create optional reranker
    reranker = None
    if config.rerank.enabled:
        from memtomem.search.reranker.factory import create_reranker

        reranker = create_reranker(config.rerank)

    # Create optional LLM provider (before SearchPipeline so it can be passed in)
    llm: LLMProvider | None = None
    if config.llm.enabled:
        from memtomem.llm.factory import create_llm

        llm = create_llm(config.llm)

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
    )

    return Components(
        config=config,
        storage=storage,
        embedder=embedder,
        index_engine=index_engine,
        search_pipeline=search_pipeline,
        llm=llm,
    )


async def close_components(comp: Components) -> None:
    """Shut down components in reverse order."""
    if comp.llm is not None:
        await comp.llm.close()
    await comp.search_pipeline.close()
    await comp.embedder.close()
    await comp.storage.close()
