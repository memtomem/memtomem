"""Application context and type aliases for the MCP server."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

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


def _require_initialized(components: Components | None, attr: str) -> Components:
    """Raise ``RuntimeError`` if ``_components`` has not been populated.

    Uses an explicit ``if … raise`` rather than ``assert`` so the check
    survives ``python -O`` and ``PYTHONOPTIMIZE`` — pre-init access is a
    programming bug we want to surface with a clear error, not an
    ``AttributeError`` the optimizer synthesizes after stripping the assert.
    """
    if components is None:
        raise RuntimeError(
            f"AppContext.{attr} accessed before ensure_initialized() — "
            "call ``await app.ensure_initialized()`` in the handler first."
        )
    return components


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

    ``_owns_components`` distinguishes two construction paths:

    * ``ensure_initialized`` — we created the ``Components`` ourselves, so
      :meth:`close` is responsible for tearing them down.
    * :meth:`from_components` — the caller supplied a ``Components`` they
      are already managing (``cli_components`` context manager, test
      fixtures); :meth:`close` must not double-close on their behalf.

    Without this flag the second path would hand the caller a footgun:
    calling ``ctx.close()`` would invalidate the ``Components`` they are
    still holding a live reference to, and the caller's own cleanup would
    then hit already-closed storage / embedder.
    """

    config: Mem2MemConfig
    webhook_manager: object | None = None
    current_namespace: str | None = None
    current_session_id: str | None = None
    # Internal state — not part of the public ``__init__`` surface; populated
    # by ``ensure_initialized`` / ``from_components`` / :meth:`set_health_watchdog`.
    _components: Components | None = field(default=None, init=False, repr=False)
    _owns_components: bool = field(default=False, init=False, repr=False)
    _dedup_scanner: DedupScanner | None = field(default=None, init=False, repr=False)
    _health_watchdog: object | None = field(default=None, init=False, repr=False)
    # per-session, scoped to AppContext lifetime. Gate to emit a dim-mismatch
    # hint only once per MCP session so repeated mem_add / mem_search calls
    # do not spam the same notice. Writes go through ``_config_lock``.
    _dim_mismatch_announced: bool = False
    _config_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _init_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    # ── component accessors ───────────────────────────────────────────────
    # These raise ``RuntimeError`` if accessed before ``ensure_initialized``
    # has populated ``_components``. Tool handlers must call
    # ``await app.ensure_initialized()`` before reading them (Phase 2). In
    # Phase 1 the lifespan eagerly inits, so all handlers see populated
    # state — the runtime check catches programming errors during the
    # migration without disappearing under ``python -O``.

    @property
    def storage(self) -> SqliteBackend:
        return _require_initialized(self._components, "storage").storage

    @property
    def embedder(self) -> EmbeddingProvider:
        return _require_initialized(self._components, "embedder").embedder

    @property
    def index_engine(self) -> IndexEngine:
        return _require_initialized(self._components, "index_engine").index_engine

    @property
    def search_pipeline(self) -> SearchPipeline:
        return _require_initialized(self._components, "search_pipeline").search_pipeline

    @property
    def llm_provider(self) -> LLMProvider | None:
        # LLM is optional even after init — return None when absent rather
        # than raising, mirroring the old field semantics.
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

    def set_health_watchdog(self, watchdog: object) -> None:
        """Stash the health-watchdog instance the lifespan has started.

        Lifespan-owned, not context-owned — :meth:`close` does not stop it.
        The indirection gives callers a typed seam instead of poking
        ``ctx._health_watchdog`` across module boundaries.
        """
        self._health_watchdog = watchdog

    # ── lifecycle ─────────────────────────────────────────────────────────

    async def ensure_initialized(self) -> Components:
        """Run ``create_components`` once, return it on subsequent calls.

        Concurrent first-callers serialize on ``_init_lock``; the first
        completes the init, later ones return the cached ``Components``.
        On failure the lock is released and ``_components`` stays ``None``,
        so a retry can succeed (transient failures like a race on DB file
        creation should not poison the context for the rest of the
        session).

        If ``create_components`` succeeds but a post-factory step
        (currently the ``DedupScanner`` construction) raises, the already-
        built ``Components`` would otherwise leak its open sqlite handle
        and embedder session. We explicitly tear it down before
        re-raising.
        """
        if self._components is not None:
            return self._components
        async with self._init_lock:
            if self._components is not None:
                return self._components
            from memtomem.search.dedup import DedupScanner
            from memtomem.server.component_factory import close_components, create_components

            comp = await create_components(self.config)
            try:
                self._dedup_scanner = DedupScanner(storage=comp.storage, embedder=comp.embedder)
            except Exception:
                # Don't leak the sqlite/embedder handles the factory opened
                # just because a post-factory step failed.
                await close_components(comp)
                raise
            self._components = comp
            self._owns_components = True
            return comp

    @classmethod
    def from_components(cls, components: Components) -> AppContext:
        """Build an ``AppContext`` from a caller-owned ``Components``.

        Used by CLI commands (``mm watchdog``) and tests that bootstrap
        components outside of the MCP server lifespan. The caller retains
        ownership — :meth:`close` will *not* tear the components down,
        since the caller (typically an ``async with cli_components()``
        block) is already responsible for that and a double-close would
        hit already-closed handles.
        """
        from memtomem.search.dedup import DedupScanner

        ctx = cls(config=components.config)
        ctx._components = components
        ctx._owns_components = False
        ctx._dedup_scanner = DedupScanner(storage=components.storage, embedder=components.embedder)
        return ctx

    async def close(self) -> None:
        """Tear down components if this context owns them.

        Webhook manager and health watchdog are owned by the lifespan, not
        the context — they are not closed here. Components passed in via
        :meth:`from_components` are also left alone (the supplier closes
        them) — the ``_owns_components`` flag distinguishes the two paths.
        """
        from memtomem.server.component_factory import close_components

        if self._components is not None and self._owns_components:
            await close_components(self._components)
        self._components = None
        self._owns_components = False
        self._dedup_scanner = None


CtxType = Context[ServerSession, AppContext] | None


def _get_app(ctx: CtxType) -> AppContext:
    # FastMCP always injects the context at call time; the None default on
    # tool signatures exists only so the param isn't positional-required.
    assert ctx is not None, "MCP framework must inject ctx at call time"
    return ctx.request_context.lifespan_context
