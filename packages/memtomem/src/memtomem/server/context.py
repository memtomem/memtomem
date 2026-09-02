"""Application context and type aliases for the MCP server."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from contextvars import ContextVar, Token
from typing import TYPE_CHECKING, Any

from mcp.server.mcpserver import Context

from memtomem.config import Mem2MemConfig
from memtomem.config_signature import Signature, build_fresh_config, current_signature
from memtomem.constants import validate_namespace

if TYPE_CHECKING:
    from memtomem._instance_registry import HeldBarrier, RegisteredInstance
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


def _same_tier(left: object, right: object) -> bool:
    """Compare one tier of index roots, ignoring how each was spelled.

    The saved config collapses the home prefix to ``~`` and stores strings,
    while a live ``IndexingConfig`` may hold ``Path`` objects — so the same
    directory arrives in two shapes. Normalizing the way
    ``IndexingConfig.all_index_roots`` does (plus ``expanduser``) keeps a
    round-trip through disk from reading as a change and triggering a
    pointless re-watch on every tool call.
    """
    return [Path(d).expanduser() for d in left] == [  # type: ignore[union-attr]
        Path(d).expanduser()
        for d in right  # type: ignore[union-attr]
    ]


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
    # Server-only opt-in for the instance registry (#1935): only the MCP
    # lifespan sets this. It deliberately lives here — NOT inside the
    # shared ``create_components`` factory — because CLI commands,
    # ``mm web``, the LangGraph integration, and quality experiments all
    # build components through that factory and must never register as
    # server instances.
    #
    # ``kw_only`` is load-bearing, not style. ``AppContext`` is a public
    # re-export whose positional order shipped in v0.3.x (``config,
    # webhook_manager, current_session_id, …``); declaring this field
    # here WITHOUT ``kw_only`` would splice a server-registration flag
    # into slot 3 of that order, and the AST guard in
    # ``test_server_instance_registration.py`` — which audits opt-ins by
    # enumerating ``register_server_instance=`` keywords — would never
    # see a positional one. Keyword-only keeps the released positional
    # surface byte-for-byte and makes the keyword the only spelling.
    register_server_instance: bool = field(default=False, kw_only=True)
    # The MCP lifespan must resolve persisted config before it decides whether
    # to create webhook/warmup services.  Mark that fully loaded instance so
    # lazy component initialisation does not apply config.d/config.json twice.
    ambient_config_loaded: bool = field(default=False, kw_only=True)
    # Lifespan reads persisted service flags without side effects so a mere MCP
    # handshake cannot run the legacy auto-discover rewrite. The owner flips
    # this after rebuilding all persisted layers and migrating at first real
    # initialization.
    defer_config_migration: bool = field(default=False, kw_only=True)
    # Backing field for the ``current_namespace`` property below. Kept off
    # ``__init__`` so callers go through the setter (which validates) and
    # cannot smuggle a hostile shape via ``AppContext(current_namespace=
    # "agent-runtime:foo:bar")``. See issue #500 for the transitive bypass
    # this property closes — direct attribute writes (Python-level bypass
    # of ``mem_ns_set``) are caught here even if a future tool were added
    # that mutates app state without re-running ``validate_namespace``.
    _current_namespace: str | None = field(default=None, init=False, repr=False)
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
    # SSE enters the SDK lifespan once per connection.  Secondary connection
    # contexts keep their session/namespace state here but delegate the
    # process-wide component graph and background services to the first
    # lifespan's owner.  This prevents one watcher/scheduler/watchdog set per
    # client while preserving per-connection session state.
    _runtime_owner: AppContext | None = field(default=None, init=False, repr=False)
    _dedup_scanner: DedupScanner | None = field(default=None, init=False, repr=False)
    _watcher: FileWatcher | None = field(default=None, init=False, repr=False)
    # Whether ``_watcher.start()`` has actually run. Degraded startup (#349)
    # constructs the watcher but leaves it stopped, so ``_watcher is not None``
    # does not mean "watching" — and ``FileWatcher`` exposes no running flag
    # while ``start()`` unconditionally builds a fresh observer + task, so a
    # second start would leak both. ``recover_from_degraded`` (#2181) needs a
    # started/not-started distinction that survives a failed start.
    _watcher_started: bool = field(default=False, init=False, repr=False)
    # Set when a failed recovery start could not be cleaned up either, so the
    # attempt may still hold a live observer + task. A retry calls ``start()``,
    # which overwrites those handles — leaving nothing able to stop them — so
    # the retry is refused and shutdown does the stopping instead.
    _watcher_cleanup_failed: bool = field(default=False, init=False, repr=False)
    # Recovery starts that failed *and* whose cleanup failed. The caller drops
    # its reference to leave the handle retryable, so these are held here for
    # ``close()`` — otherwise a half-built loop outlives every reference to it.
    _failed_services: list[tuple[object, str]] = field(default_factory=list, init=False, repr=False)
    # Last on-disk config state this context reconciled its watched roots
    # against (#2186). ``None`` until ``ensure_initialized`` seeds it.
    _config_signature: Signature | None = field(default=None, init=False, repr=False)
    # Serializes root reconciliation. Deliberately its own lock: the work spans
    # a stat, a parse, a live-config mutation and a watcher call, and putting
    # that behind ``_config_lock`` (held across the embedding-reset swap) or
    # ``_init_lock`` would entangle it with those orderings for no benefit.
    _watch_roots_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _scheduler: object | None = field(default=None, init=False, repr=False)
    _policy_scheduler: object | None = field(default=None, init=False, repr=False)
    _health_watchdog: object | None = field(default=None, init=False, repr=False)
    # Lifespan-owned model-warmup task (#1621, ``config.warmup.enabled``).
    # Held here rather than in ``app_lifespan`` so the task keeps a strong
    # reference (fire-and-forget tasks are otherwise GC-collectable) and
    # so ``close()`` can cancel an in-flight warmup *before* components
    # shut down under it. ``from_components`` contexts leave it ``None``.
    _warmup_task: asyncio.Task | None = field(default=None, init=False, repr=False)
    # Held instance-registry sentinel (#1935); populated only when
    # ``register_server_instance`` is set and storage init succeeded.
    _instance_registration: RegisteredInstance | None = field(default=None, init=False, repr=False)
    # Held lifecycle barrier (#1936), taken *before* storage opens and
    # kept for the process lifetime — released on the same confirmed-close
    # gate as the sentinel above. Holding it past sentinel publication is
    # what covers the case where registration itself failed: the store is
    # open, nothing advertises it, and only this hold refuses uninstall.
    _lifecycle_barrier: HeldBarrier | None = field(default=None, init=False, repr=False)
    # per-session, scoped to AppContext lifetime. Gate to emit a dim-mismatch
    # hint only once per MCP session so repeated mem_add / mem_search calls
    # do not spam the same notice. Writes go through ``_config_lock``.
    _dim_mismatch_announced: bool = False
    _config_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _init_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Guards mutations of ``current_session_id`` + ``current_agent_id`` and
    # ``_ending_session_ids``.
    # Kept distinct from ``_config_lock`` so a long-running config write
    # cannot block a session start, and vice versa.
    _session_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Per-resolved-source-file locks that serialize the memory-file
    # read → rewrite → re-index → rollback span in the MCP CRUD tools
    # (``mem_edit`` / ``mem_delete`` / ``mem_add`` / ``mem_batch_add``).
    # Without this, two concurrent tool calls on chunks in the same file
    # each snapshot the same pre-image and stale line range, then clobber
    # each other or splice over an unrelated entry (issue #1570).
    #
    # This is level L1 of the memory-file lock order (see the ``context._atomic``
    # module docstring): in-process, serializing concurrent CRUD within a single
    # MCP server process/event loop. Cross-process races (a second MCP server,
    # the CLI, ``mm web``) and CRUD-vs-``memory-migrate`` are closed by L2 — the
    # per-file cross-process sidecar, held across the whole span via
    # ``async_file_lock`` and passed down to ``index_file(lock_held=True)`` so
    # the nested engine acquire is skipped instead of self-deadlocking (#1587).
    # The CRUD tools acquire L1 then L2; this L1 lock is still needed on its own
    # for the DB-only bulk ``mem_delete`` namespace branch (no sidecar there).
    #
    # A plain ``dict`` (not the ``web/routes/_locks.py`` per-loop proxy) is
    # correct because an ``AppContext`` never outlives one event loop —
    # ``from_components`` builds a fresh context per test/run, and
    # ``asyncio.Lock`` binds its loop at first acquire on py312. Keys are
    # canonicalized paths (``memory_file_lock_key``), bounded by the file
    # count, so the dict is not evicted (pruning would race a waiter that
    # already holds a ref).
    _memory_file_locks: dict[str, asyncio.Lock] = field(
        default_factory=dict, init=False, repr=False
    )
    # Sessions whose ``mem_session_end`` effectful phase (billable LLM
    # auto-summary, archive-chunk write) is in flight (issue #1571). This is
    # the at-most-once *claim* — a second/retried end whose session id is
    # already here returns "No active session." and does not re-run the
    # phase. Kept separate from ``current_session_id`` so the claim does NOT
    # null the public session handle at entry: nulling it early would make
    # concurrent writes lose ``mem_add`` agent-namespace routing and
    # ``mem_scratch_set`` binding for the whole multi-second phase. Provenance
    # capture treats membership in this set as a seal, so those later writes
    # keep their routing context without joining the closing session. The
    # handle is nulled only when the phase completes. Guarded by
    # ``_session_lock``.
    _ending_session_ids: set[str] = field(default_factory=set, init=False, repr=False)

    # Serializes a whole session *transition* — start (including the inline
    # auto-end of a superseded session) and end. Distinct from
    # ``_session_lock``, which guards individual field mutations and must be
    # released across the awaits a transition makes (draining writes, reading
    # events, the DB writes). ``_ending_session_ids`` alone is not enough: it
    # makes ending at-most-once, but nothing about it decides *which* new
    # session wins when two starts interleave, so one start could overwrite
    # or orphan the session another just created.
    _session_transition_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock, init=False, repr=False
    )
    # In-flight session-bound writes. A session teardown reads the session's
    # events to build its summary and counts; a write admitted before the
    # teardown but still persisting would be missed by that read. The ledger
    # below lets teardown wait for exactly those writes to land.
    #
    # Each admission takes a monotonically increasing ticket. A drain records
    # the highest ticket issued so far as its boundary and waits only for
    # still-open tickets at or below it — writes admitted *after* the drain
    # started are none of its business, and waiting for them could stall
    # teardown indefinitely under a steady write stream.
    #
    # The tickets exist because a bare "is anything in flight" flag cannot be
    # waited on safely. ``asyncio.Event.wait()`` returns as soon as it is
    # woken and never re-checks: writer A can set the event, and writer B can
    # clear it again before the waiter is scheduled, leaving the waiter to
    # report a successful drain while B is still running. A ``Condition``
    # re-tests its predicate on every wakeup, which closes that hole.
    #
    # The condition shares ``_session_lock``, so admission and completion are
    # already serialized against the handle mutations next to them.
    _write_ticket_seq: int = field(default=0, init=False, repr=False)
    _open_write_tickets: set[int] = field(default_factory=set, init=False, repr=False)
    # Built in __post_init__ rather than a default_factory: it has to wrap
    # ``_session_lock``, and a factory cannot see sibling fields.
    _writes_done: asyncio.Condition = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._writes_done = asyncio.Condition(self._session_lock)

    def __getattribute__(self, name: str) -> Any:
        """Keep process-wide config reads attached to the runtime owner.

        SSE connection contexts share one component graph. A config rollback
        may replace the configuration object rather than mutate it, so a
        copied reference here would leave sibling connections on stale state.
        """
        if name == "config":
            try:
                owner = object.__getattribute__(self, "_runtime_owner")
            except AttributeError:
                # Dataclass ``__init__`` assigns config before internal fields.
                owner = None
            if owner is not None:
                return owner.config
        return object.__getattribute__(self, name)

    def __setattr__(self, name: str, value: Any) -> None:
        """Route config replacement through the shared runtime owner."""
        if name == "config":
            try:
                owner = object.__getattribute__(self, "_runtime_owner")
            except AttributeError:
                owner = None
            if owner is not None:
                owner.config = value
                # Preserve the latest value if this facade is later detached
                # during connection shutdown.
                object.__setattr__(self, name, value)
                return
        object.__setattr__(self, name, value)

    # ── current_namespace (validated) ─────────────────────────────────────
    # Property + setter pair so every write — whether via ``mem_ns_set``,
    # a future tool we forget to gate, or a bare ``app.current_namespace =
    # X`` from test code or a Python adapter — runs through
    # :func:`validate_namespace`. ``mem_session_start``'s priority chain
    # (priority 3) reads this back as a fallback, so a hostile string here
    # round-trips into the ``sessions`` row — exactly the bypass issue
    # #500 was filed to close.
    #
    # Defense-in-depth on top of the input gates at every public surface
    # (``mem_ns_set``, ``mem_session_start(namespace=)``,
    # ``mem_agent_share(target=)`` …). The forward-shield contract of
    # :func:`validate_namespace` (constants.py:96-100) still holds: this
    # property only re-validates **caller-supplied** writes that reach app
    # state. Internal callers that read the value back never re-validate.

    @property
    def current_namespace(self) -> str | None:
        return self._current_namespace

    @current_namespace.setter
    def current_namespace(self, value: str | None) -> None:
        if value is not None:
            validate_namespace(value)
        self._current_namespace = value

    @staticmethod
    def memory_file_lock_key(path: Path) -> str:
        """Canonical lock key for a memory file: resolved, then case-folded.

        Case-folding is unconditional: on case-insensitive filesystems
        (macOS APFS default, Windows NTFS) ``Notes.md`` and ``notes.md``
        are the *same* file and must share one lock or the #1570
        corruption recurs across the two spellings. On case-sensitive
        filesystems the fold merely makes two genuinely distinct files
        share a lock — needless serialization, never corruption.
        """
        return str(path.expanduser().resolve()).casefold()

    def get_memory_file_lock(self, path: Path | str) -> asyncio.Lock:
        """Return the per-file ``asyncio.Lock`` for ``path`` (issue #1570).

        Callers hold this across a memory file's whole
        read → rewrite → re-index → rollback span so concurrent MCP CRUD
        tools cannot lose updates or splice over a stale line range.
        ``path`` may be a ``Path`` (canonicalized here) or a ``str`` that
        MUST already come from :meth:`memory_file_lock_key` — passing a raw
        string path would silently key a second lock for the same file.
        Get-or-create is race-free: a single event loop never suspends
        between the ``dict`` read and write below.

        This is L1 in the memory-file lock order (``context._atomic``). L2 —
        the cross-process sidecar — is acquired *inside* this lock via
        ``async_file_lock`` (never the blocking ``_file_lock`` on the loop) and
        passed to ``index_file(lock_held=True)`` so the engine's own sidecar
        acquire is skipped rather than self-deadlocking (#1587). Acquire order
        is always L1 → L2 → L3 (``_index_lock``); never the reverse.
        """
        key = path if isinstance(path, str) else self.memory_file_lock_key(path)
        lock = self._memory_file_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._memory_file_locks[key] = lock
        return lock

    # ── in-flight session-bound writes ────────────────────────────────────

    @contextlib.asynccontextmanager
    async def write_in_flight(self):
        """Mark a session-bound write as in flight for its whole span.

        Wrap the *entire* write — capturing the active session, indexing,
        and any bookkeeping the teardown will later read back. Exiting
        early, say right after indexing, reopens the gap this closes: a
        teardown could observe idle, snapshot the session's events, and
        miss a row written a moment later.

        ``_session_lock`` is held only to take and release the ticket,
        never across the wrapped body — a writer that held it would
        deadlock against the very teardown waiting for that writer.
        """
        async with self._writes_done:  # acquires _session_lock
            self._write_ticket_seq += 1
            ticket = self._write_ticket_seq
            self._open_write_tickets.add(ticket)
        try:
            yield
        finally:
            async with self._writes_done:
                self._open_write_tickets.discard(ticket)
                self._writes_done.notify_all()

    async def wait_writes_drained(self, timeout: float) -> bool:
        """Wait for the session-bound writes already in flight to land.

        Only writes admitted *before* this call are waited on. A write
        that starts afterwards is none of this drain's business — waiting
        for those too would let a steady write stream stall a teardown
        forever.

        Returns whether the wait succeeded; a timeout is reported, never
        raised, so a slow or stuck write degrades a teardown's inputs
        instead of failing the teardown itself. Must be awaited *outside*
        ``_session_lock`` — writers need that lock to release their
        ticket.
        """

        async def _drain() -> None:
            async with self._writes_done:
                boundary = self._write_ticket_seq
                while any(t <= boundary for t in self._open_write_tickets):
                    await self._writes_done.wait()

        try:
            await asyncio.wait_for(_drain(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(
                "session_write_drain_timeout open_writes=%d timeout=%.1fs",
                len(self._open_write_tickets),
                timeout,
            )
            return False
        return True

    # ── component accessors ───────────────────────────────────────────────
    # These raise ``RuntimeError`` if accessed before ``ensure_initialized``
    # has populated ``_components``. Tool handlers reach the context via
    # ``_get_app_initialized`` (which awaits ``ensure_initialized``), so
    # the guard catches programming errors — a handler accidentally going
    # through ``_get_app`` and reading a property before init — without
    # disappearing under ``python -O`` the way ``assert`` would.

    @property
    def storage(self) -> SqliteBackend:
        return _require_initialized(self._runtime_components(), "storage").storage

    @property
    def embedder(self) -> EmbeddingProvider:
        return _require_initialized(self._runtime_components(), "embedder").embedder

    @property
    def index_engine(self) -> IndexEngine:
        return _require_initialized(self._runtime_components(), "index_engine").index_engine

    @property
    def search_pipeline(self) -> SearchPipeline:
        return _require_initialized(self._runtime_components(), "search_pipeline").search_pipeline

    @property
    def llm_provider(self) -> LLMProvider | None:
        # LLM is optional even after init — return None when absent rather
        # than raising, mirroring the old field semantics.
        components = self._runtime_components()
        return None if components is None else components.llm

    @property
    def dedup_scanner(self) -> DedupScanner | None:
        owner = self._runtime_owner
        return owner._dedup_scanner if owner is not None else self._dedup_scanner

    @property
    def health_watchdog(self) -> object | None:
        owner = self._runtime_owner
        return owner._health_watchdog if owner is not None else self._health_watchdog

    @property
    def embedding_broken(self) -> dict | None:
        # Mirrors the old field: None until init has run, then either None
        # (healthy) or the mismatch-info dict (degraded mode, see #349).
        components = self._runtime_components()
        if components is None:
            return None
        return components.embedding_broken

    def _runtime_components(self) -> Components | None:
        owner = self._runtime_owner
        return owner._components if owner is not None else self._components

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
        if self._runtime_owner is not None:
            try:
                comp = await self._runtime_owner.ensure_initialized()
            finally:
                # ``from_runtime_owner`` copies these two by value, and a
                # facade built before first initialization captured the
                # handshake-era pair. The owner replaces both when it runs the
                # deferred config rebuild, so a facade that skipped this would
                # keep firing on a manager the owner has already closed — and
                # answer from a config the migration superseded. In ``finally``
                # because the rebuild happens *before* components are built: a
                # failure past that point still leaves the old manager closed.
                self.config = self._runtime_owner.config
                self.webhook_manager = self._runtime_owner.webhook_manager
            # Keep the private compatibility handle in sync for the one
            # embedding-reset implementation that intentionally mutates the
            # shared Components container directly.
            self._components = comp
            self._watcher = self._runtime_owner._watcher
            return comp
        if self._components is not None:
            # Already initialized: this call is a handler entering, which is
            # the moment to notice that another process changed the index roots
            # (#2186). Kept here rather than in ``_get_app_initialized`` so it
            # rides the same chokepoint every caller already goes through —
            # including handlers that reach the context without a ``ctx``.
            await self.reconcile_watched_roots()
            return self._components
        async with self._init_lock:
            if self._components is not None:
                return self._components
            from memtomem.indexing.watcher import FileWatcher
            from memtomem.search.dedup import DedupScanner
            from memtomem.server.component_factory import close_components, create_components

            # Sample the config signature *before* any loader reads the files.
            # A write landing mid-load is then noticed by the next reconcile
            # rather than being recorded as already applied (#2186). Sampling
            # early can only cost one redundant reconcile pass.
            signature_at_load = current_signature()

            if self.defer_config_migration:
                # Handshake discovery is read-only, so its config may already
                # be stale when the first real tool call arrives. Rebuild from
                # defaults + environment + config.d + config.json before
                # running the deferred migration; replaying config.json alone
                # would bank a changed fragment's signature while retaining
                # its handshake-era values. Keep startup's tolerant override
                # behavior so malformed config remains repairable.
                self.config = build_fresh_config(migrate=True, strict_overrides=False)
                self.ambient_config_loaded = True
                await self._reconcile_webhook_manager()

            # #1936: take the lifecycle barrier BEFORE storage opens, so a
            # concurrent ``mm uninstall`` either has not started staging
            # (we hold shared, its exclusive acquire refuses) or is already
            # staging (our acquire refuses and the store is never opened).
            # Failure propagates — proceeding here on an unproven barrier
            # would reopen exactly the TOCTOU this closes.
            if self.register_server_instance and self._lifecycle_barrier is None:
                await self._acquire_lifecycle_barrier()

            comp = await create_components(
                self.config,
                load_ambient_config=not self.ambient_config_loaded,
            )
            # Expose storage/embedder via the property accessors *before*
            # constructing schedulers — they reach into ``ctx.storage`` etc.,
            # and ``_require_initialized`` would raise without this. The
            # except-block below rolls the flag back on failure so a retry
            # isn't blocked by half-built state.
            self._components = comp
            self._owns_components = True

            dedup: DedupScanner | None = None
            watcher: FileWatcher | None = None
            watcher_started = False
            scheduler: object | None = None
            policy_scheduler: object | None = None
            watchdog: object | None = None
            try:
                # #1935: advertise this server in the instance registry once
                # storage is provably open (the DB file exists and this is
                # the finalized config). Registration itself never raises;
                # settlement re-raises a caught cancellation so the rollback
                # below runs with the published sentinel recoverable.
                if self.register_server_instance:
                    await self._acquire_instance_registration(comp)

                dedup = DedupScanner(
                    storage=comp.storage,
                    embedder=comp.embedder,
                    # ``getattr``: callers hand in their own ``Components``
                    # (``from_components``, CLI, focused tests), and a
                    # stand-in without the field should leave the scanner
                    # unleased rather than fail construction.
                    generation=getattr(comp, "generation", None),
                )

                # Skip background loops in degraded mode (issue #349) — the
                # watcher/schedulers/watchdog walk the index or re-embed
                # chunks and would crash on the missing ``chunks_vec`` table.
                # Recovery happens via ``mem_embedding_reset``, which calls
                # :meth:`recover_from_degraded` to start what was skipped here
                # without waiting for a restart (#2181).
                watcher = FileWatcher(
                    comp.index_engine,
                    self.config.indexing,
                    search_pipeline=comp.search_pipeline,
                )
                if comp.embedding_broken is None:
                    await watcher.start()
                    watcher_started = True

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

                    watchdog = HealthWatchdog(
                        self,
                        self.config.health_watchdog,
                        self.config.scheduler,
                    )
                    await watchdog.start()

                # P2 cron Phase A footgun: scheduler enabled without watchdog
                # means schedules will silently never fire. Loud at startup
                # (warning, not debug) per feedback_silent_except_log_level.
                if self.config.scheduler.enabled and not self.config.health_watchdog.enabled:
                    logger.warning(
                        "scheduler.enabled=True but health_watchdog.enabled=False — "
                        "schedules will not dispatch (scheduler rides the watchdog tick)"
                    )
                if (
                    self.config.scheduler.enabled
                    and self.config.scheduler.default_timezone.lower() != "utc"
                ):
                    logger.warning(
                        "scheduler.default_timezone=%r — Phase A only honors 'utc'; "
                        "falling back to UTC",
                        self.config.scheduler.default_timezone,
                    )
            except BaseException:
                # Same accumulate-and-defer discipline as ``close()``: a
                # cancellation arriving during one of these stops must not
                # skip the component close and sentinel settlement below.
                # The original exception re-raises regardless (when the
                # trigger *was* a cancellation, it is that same instance).
                for resource, stage in (
                    (watchdog, "health_watchdog"),
                    (policy_scheduler, "policy_scheduler"),
                    (scheduler, "scheduler"),
                    (watcher, "watcher"),
                ):
                    try:
                        await _stop_quietly(resource, stage)
                    except asyncio.CancelledError:
                        logger.warning("rollback stop '%s' cancelled — continuing", stage)
                teardown = await close_components(comp)
                # Release the sentinel only when storage provably closed —
                # a failed close leaves a possibly-open store, which must
                # stay advertised (#1935).
                await self._release_instance_registration(teardown.storage_closed)
                self._release_lifecycle_barrier(teardown.storage_closed)
                self._components = None
                self._owns_components = False
                raise

            self._dedup_scanner = dedup
            self._config_signature = signature_at_load
            # Cleared here, where initialization commits, and not next to the
            # rebuild above. Everything between the two can raise — the webhook
            # close is a cancellation point, and so is every component start —
            # and clearing it eagerly made that abort permanent: the retry
            # skipped the rebuild, kept the config from the aborted attempt,
            # and then banked *this* signature over it. A config edit made in
            # between was recorded as applied while never being read.
            self.defer_config_migration = False
            self._watcher = watcher
            self._watcher_started = watcher_started
            self._scheduler = scheduler
            self._policy_scheduler = policy_scheduler
            self._health_watchdog = watchdog
            return comp

    async def recover_from_degraded(self) -> None:
        """Start the background loops a degraded startup skipped (#2181).

        Called by ``mem_embedding_reset`` (both ``apply_current`` and
        ``revert_to_stored``) once the embedding mismatch is cleared. Degraded
        startup (#349) leaves the file watcher constructed-but-stopped and
        never builds the consolidation/policy schedulers or the health
        watchdog; without this fanout they stay down — no auto-indexing, no
        maintenance — until the server restarts.

        The tool does not reach into those services itself: this context owns
        them, so it owns their recovery.

        Recovery is *per service* and **retryable**. Each start is guarded and
        assigned only on success, so a failure leaves that service missing and
        a later reset call tries again — whereas one "already recovered" flag
        would burn the only attempt. A fully recovered context no-ops on the
        same guards, so a second reset never starts a duplicate loop.

        Failures are logged, not raised: the recovery the user asked for (a
        working index) has already happened by the time this runs, and losing
        the scheduler must not turn a successful reset into a failed tool
        call. Same trade-off as the retirement closes in ``revert_to_stored``.
        """
        if self._runtime_owner is not None:
            await self._runtime_owner.recover_from_degraded()
            self._components = self._runtime_owner._components
            self._watcher = self._runtime_owner._watcher
            return
        async with self._init_lock:
            comp = self._components
            if comp is None:
                return
            # ``from_components`` contexts (CLI, tests) deliberately run no
            # background loops — growing them here would hand a caller-owned
            # components object loops it never asked for and does not close.
            if not self._owns_components:
                comp.embedding_broken = None
                return

            # The snapshot is a startup reading that nothing else ever clears;
            # stale, it keeps status surfaces reporting degraded after a
            # successful reset. Clearing it is *not* the idempotency gate —
            # the per-service guards below are.
            comp.embedding_broken = None

            # Keep the gates and constructor arguments below in step with
            # ``ensure_initialized``. They are deliberately not extracted into
            # a shared helper: init needs all-or-nothing rollback of a whole
            # batch, recovery needs incremental best-effort starts, and one
            # helper serving both would have to carry both failure policies.
            if (
                self._watcher is not None
                and not self._watcher_started
                and not self._watcher_cleanup_failed
            ):
                # The start, its logging and its failed-start cleanup live in
                # ``indexing/watcher.py`` because ``mm web`` performs the same
                # recovery on a watcher it holds differently (#2188). Only the
                # bookkeeping is this context's.
                from memtomem.indexing.watcher import WatcherResumer

                resumer = WatcherResumer(
                    self._watcher,
                    started=self._watcher_started,
                    can_retry=not self._watcher_cleanup_failed,
                )
                try:
                    await resumer.resume()
                finally:
                    # In ``finally`` because a cancellation mid-start still
                    # settles whether the instance may be started again, and
                    # losing that would let a later reset start over handles
                    # nothing can stop.
                    self._watcher_started = resumer.started
                    self._watcher_cleanup_failed = not resumer.can_retry

            if self.config.consolidation_schedule.enabled and self._scheduler is None:
                from memtomem.server.scheduler import ConsolidationScheduler

                scheduler = ConsolidationScheduler(self, self.config.consolidation_schedule)
                if await self._start_recovered_service(scheduler, "consolidation scheduler"):
                    self._scheduler = scheduler

            if self.config.policy.enabled and self._policy_scheduler is None:
                from memtomem.server.scheduler import PolicyScheduler

                policy_scheduler = PolicyScheduler(self, self.config.policy)
                if await self._start_recovered_service(policy_scheduler, "policy scheduler"):
                    self._policy_scheduler = policy_scheduler

            if self.config.health_watchdog.enabled and self._health_watchdog is None:
                from memtomem.server.health_watchdog import HealthWatchdog

                watchdog = HealthWatchdog(self, self.config.health_watchdog, self.config.scheduler)
                if await self._start_recovered_service(watchdog, "health watchdog"):
                    self._health_watchdog = watchdog

    async def _start_recovered_service(self, service: Any, label: str) -> bool:
        """Start one recovered background service; ``True`` when it is running.

        A failed start is stopped again so a half-built loop does not linger,
        and reported ``False`` so the caller leaves its handle ``None`` — that
        missing handle is what makes the next reset retry.

        When even the stop fails, the instance is kept in ``_failed_services``
        instead of being dropped: the caller is about to forget its only
        reference, and something whose shutdown never completed still has to
        be reachable from :meth:`close`.
        """
        try:
            await service.start()
        except asyncio.CancelledError:
            await self._quarantine_failed_start(service, label)
            raise
        except Exception:
            logger.warning(
                "Failed to start the %s after embedding recovery — "
                "it stays down until the next reset or restart",
                label,
                exc_info=True,
            )
            # A cancellation arriving during this cleanup propagates rather
            # than being swallowed: swallowing it would let a cancelled reset
            # keep starting the remaining services and return as if nothing
            # had happened.
            await self._quarantine_failed_start(service, label)
            return False
        return True

    async def _reconcile_webhook_manager(self) -> None:
        """Rebuild the webhook manager against the refreshed config.

        The handshake builds the manager from a config read before migration —
        the very staleness the deferred rebuild above exists to correct. Left
        alone, a manager built at handshake keeps firing at the URL it captured
        then, so a webhook the user disabled or repointed between handshake and
        the first tool call still delivers to the endpoint they removed. That
        is a config change silently not taking effect on an egress path, which
        is why this reconciles rather than waiting for the next process start.

        Unconditional rather than diffed: this runs once per process, and
        ``WebhookManager`` rewrites its own config when it rejects a URL, so a
        equality check against ``config.webhook`` would not mean what it reads
        like.
        """
        from memtomem.server.webhooks import WebhookManager

        webhook = self.config.webhook
        replacement: object | None = None
        if webhook.enabled and webhook.url:
            replacement = WebhookManager(webhook)
        previous, self.webhook_manager = self.webhook_manager, replacement
        if previous is None:
            return
        # The replacement is published *before* the old close is awaited.
        # ``_stop_quietly`` re-raises ``CancelledError``, so clearing the field
        # first and closing after would drop the only reference to a manager
        # that never closed — teardown reads this field and would find the
        # replacement, or nothing. On that cancellation the old manager goes to
        # the same retry channel a failed recovery start uses, so ``close``
        # still stops it. (An ordinary close failure is logged and swallowed by
        # ``_stop_quietly``, as it is for every other resource here.)
        try:
            await _stop_quietly(previous, "webhook_manager")
        except BaseException:
            self._failed_services.append((previous, "webhook_manager"))
            raise

    async def reconcile_watched_roots(self) -> None:
        """Re-watch the index roots when someone else edited the config (#2186).

        The MCP server built its ``FileWatcher`` once, at init; nothing since
        told it that ``mm init``, ``mm mem init``, ``mm config unset``, the web
        UI, or a hand edit added or removed a memory dir. Those all just rewrite
        ``~/.memtomem/config.json`` in another process — so the only way to
        notice is to look at the file, which is what this does, once per tool
        call, at the cost of a few ``stat`` calls (see
        :func:`~memtomem.config_signature.current_signature`).

        Reconciling the roots deliberately does *not* reload the rest of the
        config: that fanout (tokenizer, reranker, embedding batch size) is the
        web's ``hot_reload.reload_if_stale`` and porting it is a separate
        change. Other fields stay as stale as they already were.

        The new roots are written onto the *live* ``IndexingConfig``, which the
        index engine and the watcher share by identity — the engine's
        within-roots guard has to accept the new directory too, or its files
        would be watched and then rejected. Note this also moves the roots the
        rest of the runtime reads (default write destination, imports, session
        archives): the reconciled unit is root policy, not just watch
        membership.
        """
        if self._runtime_owner is not None:
            await self._runtime_owner.reconcile_watched_roots()
            self._components = self._runtime_owner._components
            self._watcher = self._runtime_owner._watcher
            return
        if self._components is None or not self._owns_components:
            return
        watcher = self._watcher
        if watcher is None:
            return

        async with self._watch_roots_lock:
            signature = current_signature()
            if signature == self._config_signature:
                return

            try:
                # ``migrate=False``: this is a read, on a hot path, of a file
                # the user owns. The legacy ``auto_discover`` migration writes
                # ``config.json``, and a tool call must not rewrite the user's
                # config as a side effect of looking at it — that write belongs
                # to startup and to the surfaces that already own it.
                #
                # ``strict_fragments``: a ``config.d`` fragment that fails to
                # parse is skipped by the loader, so the roots it declared
                # would come back missing and be read here as a removal.
                #
                # The signature banked below stays the one sampled *before*
                # this read: a write landing mid-read is not in ``fresh``, and
                # banking a signature newer than the config it describes would
                # mark that write applied and never look at it again.
                fresh = build_fresh_config(migrate=False, strict_fragments=True)
            except Exception:
                # A broken config file must not be read as "the user removed
                # every root" — ``build_fresh_config`` is strict for exactly
                # that reason. Bank the signature so the same broken file is
                # not re-parsed on every subsequent tool call; fixing the file
                # changes its mtime, which lets the retry through.
                self._config_signature = signature
                logger.warning(
                    "Could not re-read the config while reconciling watched roots — "
                    "keeping the current root set",
                    exc_info=True,
                )
                return

            indexing = self.config.indexing
            # Compare the tiers separately, not the flattened roots. Moving a
            # directory from ``memory_dirs`` to ``project_memory_dirs`` leaves
            # the flattened list identical while changing what the directory
            # *is*: the engine classifies scope off ``project_memory_dirs``
            # (``IndexEngine._resolve_scope`` → ``classify_scope``), so a
            # missed reclassification keeps writing project-shared content
            # under user-tier rules.
            if _same_tier(fresh.indexing.memory_dirs, indexing.memory_dirs) and _same_tier(
                fresh.indexing.project_memory_dirs, indexing.project_memory_dirs
            ):
                self._config_signature = signature
                return

            previous_memory_dirs = list(indexing.memory_dirs)
            previous_project_dirs = list(indexing.project_memory_dirs)
            indexing.memory_dirs = list(fresh.indexing.memory_dirs)
            indexing.project_memory_dirs = list(fresh.indexing.project_memory_dirs)
            try:
                await watcher.reconfigure(indexing)
            except Exception:
                # ``reconfigure`` restores its own watch set, so leaving the new
                # roots on the shared config would desync the engine from the
                # watcher — the engine would accept files in a directory nobody
                # watches. Put the roots back and leave the signature unbanked
                # so the next tool call tries again rather than freezing the
                # watch set until the user edits the file a second time.
                indexing.memory_dirs = previous_memory_dirs
                indexing.project_memory_dirs = previous_project_dirs
                logger.warning(
                    "Failed to reconcile watched roots after a config change — "
                    "still watching the previous set",
                    exc_info=True,
                )
                return

            self._config_signature = signature
            logger.info(
                "Reconciled watched index roots after a config change: %d root(s)",
                len(indexing.all_index_roots()),
            )

    async def _quarantine_failed_start(self, service: Any, label: str) -> None:
        """Stop a service whose start failed; retain it if the stop fails too."""
        stop = getattr(service, "stop", None) or getattr(service, "close", None)
        if stop is None:
            return
        try:
            await stop()
        except asyncio.CancelledError:
            logger.warning("Recovery cleanup of the %s was cancelled", label)
            self._failed_services.append((service, label))
            raise
        except Exception:
            logger.warning(
                "Recovery cleanup of the %s failed — retaining it for shutdown",
                label,
                exc_info=True,
            )
            self._failed_services.append((service, label))

    async def _acquire_lifecycle_barrier(self) -> None:
        """Take the shared lifecycle barrier before storage opens (#1936).

        Offloaded via ``asyncio.to_thread`` (bounded cross-process lock —
        forbidden directly on the event loop) and settled through
        :func:`settle_shielded_value`, *not* ``settle_shielded_result``:
        that variant swallows worker failures, which here would let a
        ``BarrierTimeout`` vanish and startup continue into
        ``create_components`` — reopening the race. Settlement hands back
        a handle acquired just as a cancellation lands so it can never be
        dropped on the floor.

        On cancellation that handle is **released**, not retained — the
        opposite of :meth:`_acquire_instance_registration`, and the
        asymmetry is deliberate. Registration happens *inside* the block
        whose rollback releases it; this runs before that block even
        starts, so storage never opened and no retry has anything to
        reuse. Retaining here would leave a barrier that outlives the
        cancelled attempt and blocks ``mm uninstall`` on behalf of a
        server that never opened the store, with no release path short of
        process exit.

        Initialization is lazy, so a failure here does not abort the MCP
        handshake: it surfaces on the first initializing tool call (and
        ``spawn_warmup`` logs it). The field stays ``None``, so a later
        call retries — correct, since the uninstall may have finished.
        """
        from memtomem._instance_registry import HeldBarrier, acquire_server_lifecycle_barrier
        from memtomem._settlement import settle_shielded_value

        future = asyncio.ensure_future(asyncio.to_thread(acquire_server_lifecycle_barrier))
        # On failure nothing was acquired — the acquire helper closes its
        # own handle — so the exception simply propagates.
        result, cancelled = await settle_shielded_value(future, what="lifecycle barrier")
        if not isinstance(result, HeldBarrier):
            # Fail closed rather than silently continuing unbarriered: the
            # settlement helper erases the result type to ``object``, so
            # only this check stands between a contract change and an
            # unprotected storage open.
            raise RuntimeError(
                f"lifecycle barrier returned {type(result).__name__}, not HeldBarrier"
            )
        if cancelled is not None:
            result.release()
            raise cancelled
        self._lifecycle_barrier = result

    def _release_lifecycle_barrier(self, storage_closed: bool) -> None:
        """Drop the barrier after a *confirmed* storage close (#1936).

        Same polarity as :meth:`_release_instance_registration`: an
        unconfirmed close leaves a possibly-open store, which must keep
        blocking uninstall until this process exits (the kernel releases
        the flock then). ``release()`` only closes a descriptor — no
        cross-process lock — so it needs no thread offload.
        """
        barrier = self._lifecycle_barrier
        if barrier is None:
            return
        if not storage_closed:
            logger.warning(
                "storage close unconfirmed — retaining lifecycle barrier %s", barrier.path
            )
            return
        self._lifecycle_barrier = None
        barrier.release()

    async def _acquire_instance_registration(self, comp: Components) -> None:
        """Register this server in the instance registry (#1935).

        Offloaded via ``asyncio.to_thread`` (the registry takes a bounded
        cross-process lock — forbidden directly on the event loop) and
        settled through :func:`settle_shielded_result`: cancelling the
        awaiting task cannot abandon a worker that is about to publish a
        sentinel. A published handle is stored *before* the caught
        cancellation re-raises, so the rollback path can release it with
        correct close-before-cleanup ordering.
        """
        from memtomem._instance_registry import RegisteredInstance, register_instance
        from memtomem._settlement import settle_shielded_result

        db_path = Path(comp.config.storage.sqlite_path).expanduser().resolve()
        future = asyncio.ensure_future(asyncio.to_thread(register_instance, db_path))
        result, cancelled = await settle_shielded_result(future, what="instance registration")
        if isinstance(result, RegisteredInstance):
            self._instance_registration = result
        if cancelled is not None:
            raise cancelled

    async def _release_instance_registration(
        self, storage_closed: bool
    ) -> asyncio.CancelledError | None:
        """Release the held sentinel after a *confirmed* storage close.

        ``storage_closed=False`` conservatively retains the registration —
        a possibly-open store must stay advertised; process-exit flock
        release remains the backstop. Returns (never raises) the first
        cancellation caught while releasing so callers can fold it into
        their own accumulate-and-defer sequencing.
        """
        reg = self._instance_registration
        if reg is None:
            return None
        if not storage_closed:
            logger.warning(
                "storage close unconfirmed — retaining instance registration %s", reg.path
            )
            return None
        self._instance_registration = None
        from memtomem._settlement import settle_shielded_result

        future = asyncio.ensure_future(asyncio.to_thread(reg.cleanup))
        _, cancelled = await settle_shielded_result(future, what="instance-registry cleanup")
        return cancelled

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
        ctx._dedup_scanner = DedupScanner(
            storage=components.storage,
            embedder=components.embedder,
            generation=getattr(components, "generation", None),
        )
        return ctx

    @classmethod
    def from_runtime_owner(cls, owner: AppContext) -> AppContext:
        """Create per-connection state backed by one process runtime owner."""
        ctx = cls(
            config=owner.config,
            webhook_manager=owner.webhook_manager,
            ambient_config_loaded=True,
        )
        ctx._runtime_owner = owner
        # These guards protect process-wide mutable state and therefore must
        # be shared even though session-transition locks remain per context.
        ctx._config_lock = owner._config_lock
        ctx._watch_roots_lock = owner._watch_roots_lock
        ctx._memory_file_locks = owner._memory_file_locks
        ctx._components = owner._components
        ctx._watcher = owner._watcher
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

        Cancellation is accumulated and deferred (#1935): a
        ``CancelledError`` caught at any stage (warmup settle, background
        loop stops, component close, sentinel release) no longer aborts
        the remaining stages — teardown always reaches the instance-
        registry settlement, and the *first* caught cancellation re-raises
        once state is cleared. Without this, a cancellation during e.g.
        the watcher stop would skip the component close and leave the
        sentinel advertising an open store.
        """
        if self._runtime_owner is not None:
            # The lifespan refcount closes the owner after the final
            # connection exits.  This context owns only request/session state.
            self._components = None
            self._watcher = None
            self._runtime_owner = None
            return

        from memtomem.server.component_factory import close_components

        first_cancel: asyncio.CancelledError | None = None

        # Cancel an in-flight warmup first so it can't race the component
        # teardown below (loading a model into a closing embedder). The
        # shielded wait blocks until the loader *thread* settles —
        # ``_warm_one`` waits for its executor future on cancellation,
        # since a thread can't be interrupted — and survives external
        # cancellation of ``close()`` itself (deferred, not swallowed).
        # The task body swallows its own errors, so no result is read.
        if self._warmup_task is not None:
            task = self._warmup_task
            task.cancel()
            while not task.done():
                try:
                    await asyncio.shield(asyncio.wait({task}))
                except asyncio.CancelledError as exc:
                    if first_cancel is None:
                        first_cancel = exc
            self._warmup_task = None

        # Recovery starts whose own cleanup failed (#2181) are stopped first:
        # they are the ones nothing else references, and a retry may have
        # already built a live replacement alongside them.
        stop_order = [(service, f"failed {label}") for service, label in self._failed_services]
        stop_order += [
            (self._health_watchdog, "health_watchdog"),
            (self._policy_scheduler, "policy_scheduler"),
            (self._scheduler, "scheduler"),
            (self._watcher, "watcher"),
        ]
        for resource, stage in stop_order:
            try:
                await _stop_quietly(resource, stage)
            except asyncio.CancelledError as exc:
                if first_cancel is None:
                    first_cancel = exc
        self._failed_services.clear()

        # Default is UNCONFIRMED: only a teardown that actually ran and
        # reported success flips this. A bare ``True`` for the
        # no-components case would let a second ``close()`` — after an
        # earlier failed storage close already cleared ``_components``
        # but retained the sentinel — release that sentinel without any
        # confirmed close. With no held registration the flag is inert
        # (release no-ops), so unflagged/from_components contexts are
        # unaffected.
        storage_closed = False
        if self._components is not None and self._owns_components:
            teardown = await close_components(self._components)
            storage_closed = teardown.storage_closed
            if first_cancel is None:
                first_cancel = teardown.cancelled
        cancelled = await self._release_instance_registration(storage_closed)
        self._release_lifecycle_barrier(storage_closed)
        if first_cancel is None:
            first_cancel = cancelled
        self._components = None
        self._owns_components = False
        self._dedup_scanner = None
        self._watcher = None
        self._watcher_started = False
        self._watcher_cleanup_failed = False
        self._scheduler = None
        self._policy_scheduler = None
        self._health_watchdog = None
        if first_cancel is not None:
            raise first_cancel


# The 2.0 SDK reordered ``Context``'s type parameters: 1.x was
# ``Context[ServerSessionT, LifespanContextT]``, 2.0 is
# ``Context[LifespanContextT, RequestT]`` — ``RequestT`` being the transport
# request object (a Starlette ``Request`` over HTTP, absent over stdio), which
# no handler here touches.
CtxType = Context[AppContext, Any] | None


def _get_app(ctx: CtxType) -> AppContext:
    # The SDK always injects the context at call time; the None default on
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


# ── Lifespan-scoped AppContext, for handlers the SDK cannot inject ─────
# The 2.0 SDK refuses to inject ``Context`` into a *static* resource handler
# (only templated ones, which carry a request), and it removed the 1.x
# ambient ``get_context()``. Static resources therefore reach the AppContext
# through this handle, which ``app_lifespan`` owns.
#
# A ContextVar, not a module global: over SSE the SDK runs the whole lowlevel
# server — lifespan included — once per *connection* (``MCPServer.sse_app``'s
# ``handle_sse``), so two overlapping clients each enter their own lifespan.
# A single global slot would let the second connection's context overwrite the
# first's, and the first disconnect would then blank the handle out from under
# a live second connection. Each connection runs in its own task, so a
# ContextVar set inside the lifespan is visible to that connection's request
# handlers (spawned beneath it) and to no one else's. stdio and
# streamable-HTTP enter the lifespan once per process and are unaffected
# either way.
_ACTIVE_APP: ContextVar[AppContext | None] = ContextVar("memtomem_active_app", default=None)


def _set_active_app(app: AppContext) -> Token[AppContext | None]:
    """Publish the lifespan's ``AppContext``; returns the reset token."""
    return _ACTIVE_APP.set(app)


def _reset_active_app(token: Token[AppContext | None]) -> None:
    """Retract what ``_set_active_app`` published. Lifespan only."""
    try:
        _ACTIVE_APP.reset(token)
    except ValueError:
        # The token belongs to another Context — only reachable if a future
        # refactor moves lifespan exit off the task that entered it. Blanking
        # the value here is still better than leaving a torn-down context
        # readable.
        _ACTIVE_APP.set(None)


async def _get_active_app_initialized() -> AppContext:
    """``_get_app_initialized`` for handlers that get no ``ctx``."""
    app = _ACTIVE_APP.get()
    if app is None:
        raise RuntimeError(
            "No active AppContext: the MCP server lifespan is not running. "
            "Handlers that receive a ctx must use _get_app_initialized(ctx)."
        )
    await app.ensure_initialized()
    return app
