"""Application context and type aliases for the MCP server."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from mcp.server.fastmcp import Context
from mcp.server.session import ServerSession

from memtomem.config import Mem2MemConfig

if TYPE_CHECKING:
    from memtomem.embedding.base import EmbeddingProvider
    from memtomem.indexing.engine import IndexEngine
    from memtomem.llm.base import LLMProvider
    from memtomem.search.dedup import DedupScanner
    from memtomem.search.pipeline import SearchPipeline
    from memtomem.server.component_factory import Components
    from memtomem.storage.sqlite_backend import SqliteBackend


@dataclass
class AppContext:
    """Dependency container for MCP request handlers.

    Heavy components (storage, embedder, index engine, search pipeline) live
    behind ``_components`` and are exposed as read-only properties. They are
    populated lazily by :meth:`ensure_initialized` so handshake-only MCP
    sessions (``initialize`` + ``tools/list``) don't trigger DB creation
    in ``~/.memtomem/``. See issue #399 for the full design.

    Phase 1 keeps ``app_lifespan`` calling ``ensure_initialized`` eagerly,
    so behavior is unchanged. Phase 3 will drop the eager call.
    """

    config: Mem2MemConfig
    webhook_manager: object | None = None
    current_namespace: str | None = None
    current_session_id: str | None = None
    # Internal state — not part of the public ``__init__`` surface; populated
    # by ``ensure_initialized`` / ``from_components`` and by the lifespan
    # (for ``_health_watchdog`` once it has started).
    _components: Components | None = field(default=None, init=False, repr=False)
    _dedup_scanner: DedupScanner | None = field(default=None, init=False, repr=False)
    _health_watchdog: object | None = field(default=None, init=False, repr=False)
    # per-session, scoped to AppContext lifetime. Gate to emit a dim-mismatch
    # hint only once per MCP session so repeated mem_add / mem_search calls
    # do not spam the same notice. Writes go through ``_config_lock``.
    _dim_mismatch_announced: bool = False
    _config_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _init_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    # ── component accessors ───────────────────────────────────────────────
    # These raise if accessed before ``ensure_initialized`` has populated
    # ``_components``. Tool handlers must call ``await app.ensure_initialized()``
    # before reading them (Phase 2). In Phase 1 the lifespan eagerly inits, so
    # all handlers see populated state — the assertions catch programming
    # errors during the migration.

    @property
    def storage(self) -> SqliteBackend:
        assert self._components is not None, (
            "AppContext.storage accessed before ensure_initialized()"
        )
        return self._components.storage

    @property
    def embedder(self) -> EmbeddingProvider:
        assert self._components is not None, (
            "AppContext.embedder accessed before ensure_initialized()"
        )
        return self._components.embedder

    @property
    def index_engine(self) -> IndexEngine:
        assert self._components is not None, (
            "AppContext.index_engine accessed before ensure_initialized()"
        )
        return self._components.index_engine

    @property
    def search_pipeline(self) -> SearchPipeline:
        assert self._components is not None, (
            "AppContext.search_pipeline accessed before ensure_initialized()"
        )
        return self._components.search_pipeline

    @property
    def llm_provider(self) -> LLMProvider | None:
        # LLM is optional even after init — return None when absent rather
        # than asserting, mirroring the old field semantics.
        return None if self._components is None else self._components.llm

    @property
    def dedup_scanner(self) -> DedupScanner | None:
        return self._dedup_scanner

    @property
    def health_watchdog(self) -> object | None:
        return self._health_watchdog

    @property
    def embedding_broken(self) -> dict | None:
        # Mirrors the old field: None until init has run, then either None
        # (healthy) or the mismatch-info dict (degraded mode, see #349).
        if self._components is None:
            return None
        return self._components.embedding_broken

    # ── lifecycle ─────────────────────────────────────────────────────────

    async def ensure_initialized(self) -> Components:
        """Run ``create_components`` once, return it on subsequent calls.

        Concurrent first-callers serialize on ``_init_lock``; the first
        completes the init, later ones return the cached ``Components``.
        On failure the lock is released and ``_components`` stays ``None``,
        so a retry can succeed (transient failures like a race on DB file
        creation should not poison the context for the rest of the
        session).
        """
        if self._components is not None:
            return self._components
        async with self._init_lock:
            if self._components is not None:
                return self._components
            from memtomem.search.dedup import DedupScanner
            from memtomem.server.component_factory import create_components

            comp = await create_components(self.config)
            self._dedup_scanner = DedupScanner(storage=comp.storage, embedder=comp.embedder)
            self._components = comp
            return comp

    @classmethod
    def from_components(cls, components: Components, **kwargs: Any) -> AppContext:
        """Build an ``AppContext`` from already-created ``Components``.

        Used by CLI commands (``mm watchdog``) and tests that bootstrap
        components outside of the MCP server lifespan.
        """
        from memtomem.search.dedup import DedupScanner

        ctx = cls(config=components.config, **kwargs)
        ctx._components = components
        ctx._dedup_scanner = DedupScanner(storage=components.storage, embedder=components.embedder)
        return ctx

    async def close(self) -> None:
        """Tear down components if they were initialized.

        Webhook manager and health watchdog are owned by the lifespan, not
        the context — they are not closed here.
        """
        from memtomem.server.component_factory import close_components

        if self._components is not None:
            await close_components(self._components)
            self._components = None
        self._dedup_scanner = None


CtxType = Context[ServerSession, AppContext] | None


def _get_app(ctx: CtxType) -> AppContext:
    # FastMCP always injects the context at call time; the None default on
    # tool signatures exists only so the param isn't positional-required.
    assert ctx is not None, "MCP framework must inject ctx at call time"
    return ctx.request_context.lifespan_context
