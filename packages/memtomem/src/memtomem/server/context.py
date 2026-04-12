"""Application context and type aliases for the MCP server."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from mcp.server.fastmcp import Context
from mcp.server.session import ServerSession

from memtomem.config import Mem2MemConfig
from memtomem.indexing.engine import IndexEngine
from memtomem.indexing.watcher import FileWatcher
from memtomem.search.dedup import DedupScanner
from memtomem.search.pipeline import SearchPipeline
from memtomem.storage.sqlite_backend import SqliteBackend

if TYPE_CHECKING:
    from memtomem.embedding.base import EmbeddingProvider
    from memtomem.llm.base import LLMProvider


@dataclass
class AppContext:
    """Dependency container holding all initialized services."""

    config: Mem2MemConfig
    storage: SqliteBackend
    embedder: EmbeddingProvider
    index_engine: IndexEngine
    search_pipeline: SearchPipeline
    watcher: FileWatcher
    dedup_scanner: DedupScanner | None = None
    webhook_manager: object | None = None
    health_watchdog: object | None = None
    llm_provider: LLMProvider | None = None
    current_namespace: str | None = None
    current_session_id: str | None = None
    _config_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


CtxType = Context[ServerSession, AppContext]


def _get_app(ctx: CtxType) -> AppContext:
    return ctx.request_context.lifespan_context
