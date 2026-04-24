"""Application context and type aliases for the MCP server."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from mcp.server.fastmcp import Context
from mcp.server.session import ServerSession

from memtomem.config import Mem2MemConfig

if TYPE_CHECKING:
    from memtomem.embedding.base import EmbeddingProvider
    from memtomem.indexing.engine import IndexEngine
    from memtomem.indexing.watcher import FileWatcher
    from memtomem.llm.base import LLMProvider
    from memtomem.search.dedup import DedupScanner
    from memtomem.search.pipeline import SearchPipeline
    from memtomem.server.component_factory import Components
    from memtomem.storage.sqlite_backend import SqliteBackend


logger = logging.getLogger(__name__)


async def _stop_quietly(resource: object | None, stage: str) -> None:
    """Stop ``resource`` and log (don't propagate) ordinary failures.

    Used by both :meth:`AppContext.ensure_initialized` failure cleanup and
    :meth:`AppContext.close` so a single resource going wrong does not
    skip the rest of the teardown sequence — shutdown must always finish
    whatever it can. ``CancelledError`` is re-raised so task cancellation
    propagates and the caller can decide whether to mask the original
    exception that triggered teardown (matches the lifespan helper from
    PR #404 / #406).
    """
    if resource is None:
        return
    stop = getattr(resource, "stop", None) or getattr(resource, "close", None)
    if stop is None:
        return
    try:
        await stop()
    except asyncio.CancelledError:
        logger.warning("Shutdown step '%s' cancelled", stage)
        raise
    except Exception:
        logger.warning("Shutdown step '%s' failed", stage, exc_info=True)


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
    # Set by ``mem_session_start(agent_id=...)`` and reset by
    # ``mem_session_end``. ``mem_agent_search(agent_id=None)`` falls back to
    # this value before falling back to ``current_namespace`` — so an agent
    # that started a session does not need to repeat its agent_id on every
    # tool call. Lives on a separate ``_session_lock`` (not ``_config_lock``)
    # because session state has a different lifetime / mutation cadence than
    # config — mixing the two locks would entangle their contention paths.
    current_agent_id: str | None = None
    # Internal state — not part of the public ``__init__`` surface; populated
    # by ``ensure_initialized`` / ``from_components``. The watcher /
    # scheduler / policy_scheduler / health_watchdog handles are populated
    # only via ``ensure_initialized`` (the lifespan path); ``from_components``
    # leaves them ``None`` because CLI commands that build a context outside
    # the MCP server don't run background loops.
    _components: Components | None = field(default=None, init=False, repr=False)
    _owns_components: bool = field(default=False, init=False, repr=False)
    _dedup_scanner: DedupScanner | None = field(default=None, init=False, repr=False)
    _watcher: FileWatcher | None = field(default=None, init=False, repr=False)
    _scheduler: object | None = field(default=None, init=False, repr=False)
    _policy_scheduler: object | None = field(default=None, init=False, repr=False)
    _health_watchdog: object | None = field(default=None, init=False, repr=False)
    # per-session, scoped to AppContext lifetime. Gate to emit a dim-mismatch
    # hint only once per MCP session so repeated mem_add / mem_search calls
    # do not spam the same notice. Writes go through ``_config_lock``.
    _dim_mismatch_announced: bool = False
    _config_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _init_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Guards mutations of ``current_session_id`` + ``current_agent_id``.
    # Kept distinct from ``_config_lock`` so a long-running config write
    # cannot block a session start, and vice versa.
    _session_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    # ── component accessors ───────────────────────────────────────────────
    # These raise ``RuntimeError`` if accessed before ``ensure_initialized``
    # has populated ``_components``. Tool handlers reach the context via
    # ``_get_app_initialized`` (which awaits ``ensure_initialized``), so
    # the guard catches programming errors — a handler accidentally going
    # through ``_get_app`` and reading a property before init — without
    # disappearing under ``python -O`` the way ``assert`` would.

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

    # ── lifecycle ─────────────────────────────────────────────────────────

    async def ensure_initialized(self) -> Components:
        """Run ``create_components`` once, return it on subsequent calls.

        Concurrent first-callers serialize on ``_init_lock``; the first
        completes the init, later ones return the cached ``Components``.
        On failure the lock is released and ``_components`` stays ``None``,
        so a retry can succeed (transient failures like a race on DB file
        creation should not poison the context for the rest of the
        session).

        After component construction this also wires up the request-path
        background loops the MCP server depends on — file watcher,
        consolidation/policy schedulers, health watchdog. Phase 3 of #399
        moved these out of ``app_lifespan`` so handshake-only sessions
        (``initialize`` + ``tools/list``) leave ``~/.memtomem/`` alone;
        the trade-off is that an idle server with zero tool calls runs
        no background maintenance — see the changelog entry for #399.

        Failure cleanup tears down whatever has already started — any
        background loops that reached ``start()``, then ``close_components``,
        then resets ``_components`` so a retry sees fresh state. This
        prevents leaking the sqlite handle, embedder session, or running
        background tasks just because a later step failed.
        """
        if self._components is not None:
            return self._components
        async with self._init_lock:
            if self._components is not None:
                return self._components
            from memtomem.indexing.watcher import FileWatcher
            from memtomem.search.dedup import DedupScanner
            from memtomem.server.component_factory import close_components, create_components

            comp = await create_components(self.config)
            # Expose storage/embedder via the property accessors *before*
            # constructing schedulers — they reach into ``ctx.storage`` etc.,
            # and ``_require_initialized`` would raise without this. The
            # except-block below rolls the flag back on failure so a retry
            # isn't blocked by half-built state.
            self._components = comp
            self._owns_components = True

            dedup: DedupScanner | None = None
            watcher: FileWatcher | None = None
            scheduler: object | None = None
            policy_scheduler: object | None = None
            watchdog: object | None = None
            try:
                dedup = DedupScanner(storage=comp.storage, embedder=comp.embedder)

                # Skip background loops in degraded mode (issue #349) — the
                # watcher/schedulers/watchdog walk the index or re-embed
                # chunks and would crash on the missing ``chunks_vec`` table.
                # Recovery happens via ``mem_embedding_reset``.
                watcher = FileWatcher(comp.index_engine, self.config.indexing)
                if comp.embedding_broken is None:
                    await watcher.start()

                degraded = comp.embedding_broken is not None

                if self.config.consolidation_schedule.enabled and not degraded:
                    from memtomem.server.scheduler import ConsolidationScheduler

                    scheduler = ConsolidationScheduler(self, self.config.consolidation_schedule)
                    await scheduler.start()

                if self.config.policy.enabled and not degraded:
                    from memtomem.server.scheduler import PolicyScheduler

                    policy_scheduler = PolicyScheduler(self, self.config.policy)
                    await policy_scheduler.start()

                if self.config.health_watchdog.enabled and not degraded:
                    from memtomem.server.health_watchdog import HealthWatchdog

                    watchdog = HealthWatchdog(self, self.config.health_watchdog)
                    await watchdog.start()
            except BaseException:
                await _stop_quietly(watchdog, "health_watchdog")
                await _stop_quietly(policy_scheduler, "policy_scheduler")
                await _stop_quietly(scheduler, "scheduler")
                await _stop_quietly(watcher, "watcher")
                await close_components(comp)
                self._components = None
                self._owns_components = False
                raise

            self._dedup_scanner = dedup
            self._watcher = watcher
            self._scheduler = scheduler
            self._policy_scheduler = policy_scheduler
            self._health_watchdog = watchdog
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

        Webhook manager is owned by the lifespan, not the context — it is
        not closed here. Components passed in via :meth:`from_components`
        are also left alone (the supplier closes them) — the
        ``_owns_components`` flag distinguishes the two paths.

        For lifespan-managed contexts this also stops the background
        loops :meth:`ensure_initialized` started (file watcher, schedulers,
        health watchdog) in reverse-allocation order so the loops drop
        their references before the storage / embedder they hold gets
        closed. Each step is wrapped via :func:`_stop_quietly` so a single
        failure does not skip the rest of the teardown sequence.

        Contexts built via :meth:`from_components` never started those
        loops, so the corresponding fields are ``None`` and the stop
        calls are no-ops.
        """
        from memtomem.server.component_factory import close_components

        await _stop_quietly(self._health_watchdog, "health_watchdog")
        await _stop_quietly(self._policy_scheduler, "policy_scheduler")
        await _stop_quietly(self._scheduler, "scheduler")
        await _stop_quietly(self._watcher, "watcher")

        if self._components is not None and self._owns_components:
            await close_components(self._components)
        self._components = None
        self._owns_components = False
        self._dedup_scanner = None
        self._watcher = None
        self._scheduler = None
        self._policy_scheduler = None
        self._health_watchdog = None


CtxType = Context[ServerSession, AppContext] | None


def _get_app(ctx: CtxType) -> AppContext:
    # FastMCP always injects the context at call time; the None default on
    # tool signatures exists only so the param isn't positional-required.
    assert ctx is not None, "MCP framework must inject ctx at call time"
    return ctx.request_context.lifespan_context


async def _get_app_initialized(ctx: CtxType) -> AppContext:
    """Fetch the ``AppContext`` and guarantee its components are populated.

    Handlers that touch storage / embedder / index_engine / search_pipeline
    must call this (not ``_get_app``) so the DB + embedder are opened on
    first use rather than at lifespan startup — that's the whole point of
    #399: an MCP handshake + ``tools/list`` leaves ``~/.memtomem/`` alone.

    After Phase 3 ``app_lifespan`` no longer calls ``ensure_initialized``,
    so any handler still reaching through ``_get_app`` would hit the
    ``_require_initialized`` guard on first property read. Phase 2 migrated
    every handler to this helper to make that flip safe.
    """
    app = _get_app(ctx)
    await app.ensure_initialized()
    return app
