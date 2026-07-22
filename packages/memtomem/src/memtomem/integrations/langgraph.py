"""LangGraph integration — use memtomem as a memory store in LangGraph agents.

Usage::

    from memtomem.integrations.langgraph import MemtomemStore

    store = MemtomemStore()

    # In a LangGraph node
    async def research_node(state):
        results = await store.search(state["query"])
        return {"context": results}

    async def save_node(state):
        await store.add(state["findings"], tags=["research"])
        return state

Multi-agent usage — bind a session to an agent identity once and let
``search`` / ``add`` derive the namespace automatically::

    await store.start_agent_session("planner")
    await store.add("our cache strategy", tags=["arch"])  # → agent-runtime:planner
    hits = await store.search("cache", include_shared=True)  # → planner + shared

The optional ``MemtomemBaseStore`` adapter implements LangGraph's tuple-
namespace ``BaseStore`` contract. It is imported lazily so the dependency-free
``MemtomemStore`` remains available in minimal installations.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self
from uuid import UUID, uuid4

from memtomem.constants import (
    AGENT_NAMESPACE_PREFIX,
    SHARED_NAMESPACE,
    normalize_bound_agent_id,
    validate_namespace,
)

__all__ = ["MemtomemBaseStore", "MemtomemStore"]

if TYPE_CHECKING:
    from memtomem.integrations.langgraph_store import MemtomemBaseStore
    from memtomem.server.component_factory import Components


def __getattr__(name: str) -> Any:
    """Lazily expose adapters that require optional dependencies."""
    if name == "MemtomemBaseStore":
        from memtomem.integrations.langgraph_store import MemtomemBaseStore

        return MemtomemBaseStore
    raise AttributeError(name)


class MemtomemStore:
    """LangGraph-compatible memory store wrapping memtomem components.

    Provides a simple async API for search, add, sessions, and working memory.
    Components are lazily initialized on first use.

    Args:
        config_overrides: Optional dict of config overrides
            (e.g. ``{"storage": {"sqlite_path": "..."}}``). Sections and
            keys that do not exist on :class:`Mem2MemConfig` raise
            ``ValueError`` from :meth:`_ensure_init` so a typo cannot
            silently land writes/index calls in the default
            ``~/.memtomem`` location. The constructor itself does not
            validate (the config object is built lazily) — the first
            ``await``-ed call surfaces the error.
    """

    def __init__(self, config_overrides: dict[str, Any] | None = None):
        self._components: Components | None = None
        self._config_overrides = config_overrides or {}
        self._current_session_id: str | None = None
        self._current_agent_id: str | None = None
        self._session_lock: asyncio.Lock = asyncio.Lock()

    async def _ensure_init(self) -> Components:
        """Initialize components on first call; return the cached instance."""
        if self._components is None:
            from memtomem.config import (
                Mem2MemConfig,
                load_config_d,
                load_config_overrides,
            )
            from memtomem.server.component_factory import create_components

            config = Mem2MemConfig()
            load_config_d(config)
            load_config_overrides(config)

            # Apply programmatic overrides after ambient config. Unknown
            # sections / keys raise immediately:
            # ``config_overrides`` is a programmatic constructor argument, so
            # a typo like ``{"storge": ...}`` or ``{"storage":
            # {"sqlite_pat": ...}}`` would otherwise fall back to the default
            # DB / memory_dirs and silently land writes in the wrong place.
            # Logging-only would be hidden by callers who don't surface
            # ``WARNING`` from ``memtomem.integrations.langgraph``.
            for section, updates in self._config_overrides.items():
                section_obj = getattr(config, section, None)
                if section_obj is None:
                    raise ValueError(
                        f"MemtomemStore.config_overrides: unknown section {section!r}. "
                        "Section must match a Mem2MemConfig section "
                        "(e.g. 'storage', 'indexing', 'embedding')."
                    )
                if not isinstance(updates, dict):
                    raise ValueError(
                        f"MemtomemStore.config_overrides: section {section!r} value "
                        f"is {type(updates).__name__}, expected dict."
                    )
                for key, value in updates.items():
                    if not hasattr(section_obj, key):
                        raise ValueError(
                            f"MemtomemStore.config_overrides: unknown key {key!r} "
                            f"in section {section!r}."
                        )
                    setattr(section_obj, key, value)

            # Ambient config was resolved above so the constructor arguments
            # remain the final, highest-precedence layer. Loading it again in
            # the factory would overwrite an isolated sqlite_path or
            # memory_dirs with ~/.memtomem settings.
            self._components = await create_components(config, load_ambient_config=False)
        return self._components

    async def close(self) -> None:
        """Close all components and release resources."""
        if self._components:
            from memtomem.server.component_factory import close_components

            await close_components(self._components)
            self._components = None

    # ── Search ────────────────────────────────────────────────────────────

    async def search(
        self,
        query: str,
        top_k: int = 10,
        namespace: str | None = None,
        source_filter: str | None = None,
        tag_filter: str | None = None,
        bm25_weight: float | None = None,
        dense_weight: float | None = None,
        include_shared: bool | None = None,
    ) -> list[dict]:
        """Search indexed memories.

        Returns list of dicts with keys: id, content, score, source, tags, namespace.

        ``include_shared`` is the multi-agent semantic toggle. State table:

        ============== ===================== =======================================
        ``include_shared`` ``_current_agent_id``  Resulting ``namespace`` filter
        ============== ===================== =======================================
        ``None`` (auto) set ("planner")        ``"agent-runtime:planner,shared"``
        ``None`` (auto) unset                  caller's ``namespace=`` (legacy)
        ``True``        set ("planner")        ``"agent-runtime:planner,shared"``
        ``True``        unset                  raises ``ValueError``
        ``False``       set ("planner")        ``"agent-runtime:planner"`` (no shared)
        ``False``       unset                  caller's ``namespace=``
        ============== ===================== =======================================

        ``True`` + no agent session is treated as a programming error
        (the caller asked to include the *shared* slice of an agent's
        view but never bound an agent) — raised explicitly so the bug
        surfaces immediately rather than degrading to a silent
        un-pinned search.
        """
        comp = await self._ensure_init()
        rrf_weights = None
        if bm25_weight is not None or dense_weight is not None:
            rrf_weights = [bm25_weight or 1.0, dense_weight or 1.0]

        effective_namespace = self._resolve_search_namespace(namespace, include_shared)

        # ADR-0011 PR-D round 9: thread project context — LangGraph
        # agents running inside a registered project should still see
        # the project's tier rows under the always-on scope filter.
        from memtomem.server.tools.search import _resolve_project_context_root

        project_context_root = _resolve_project_context_root(comp)

        results, stats = await comp.search_pipeline.search(
            query=query,
            top_k=top_k,
            namespace=effective_namespace,
            source_filter=source_filter,
            tag_filter=tag_filter,
            rrf_weights=rrf_weights,
            project_context_root=project_context_root,
        )
        return [
            {
                "id": str(r.chunk.id),
                "content": r.chunk.content,
                "score": r.score,
                "source": str(r.chunk.metadata.source_file),
                "tags": list(r.chunk.metadata.tags),
                "namespace": r.chunk.metadata.namespace,
                "rank": r.rank,
            }
            for r in results
        ]

    def _resolve_search_namespace(
        self, namespace: str | None, include_shared: bool | None
    ) -> str | None:
        """Translate ``include_shared`` + bound agent into a namespace filter.

        Public contract is documented in ``search``'s docstring; this helper
        only encodes the lookup table so it can be unit-tested without
        spinning up components.

        ``self._current_agent_id`` is concatenated into ``AGENT_NAMESPACE_PREFIX``
        without re-validation here: ``start_agent_session`` is the sole writer
        of that field and runs ``normalize_bound_agent_id`` before binding, so
        any value that reaches this point is already gate-checked — and is
        never the reserved ``"default"``, which binds ``None`` instead (#1875).
        """

        if include_shared is True and self._current_agent_id is None:
            raise ValueError(
                "include_shared=True requires an active agent session. "
                "Call start_agent_session(agent_id) first or set include_shared=False."
            )
        if include_shared is False and self._current_agent_id is not None:
            return f"{AGENT_NAMESPACE_PREFIX}{self._current_agent_id}"
        if include_shared in (None, True) and self._current_agent_id is not None:
            return f"{AGENT_NAMESPACE_PREFIX}{self._current_agent_id},{SHARED_NAMESPACE}"
        # No agent bound and the caller did not force include_shared=True →
        # fall back to whatever the caller passed (legacy behaviour).
        return namespace

    def _resolve_add_namespace(self, namespace: str | None) -> str | None:
        """Default the ``add`` namespace to the bound agent's private bucket.

        If the caller passes an explicit ``namespace`` it wins (escape hatch
        for "I want to write to ``shared`` while my session is bound to
        ``planner``"). Otherwise, when an agent session is active, writes
        land in ``agent-runtime:<id>``.

        ``self._current_agent_id`` reaches the concat path pre-validated —
        ``start_agent_session`` is the only writer and runs
        ``normalize_bound_agent_id`` before binding (same invariant as
        ``_resolve_search_namespace``).
        """

        if namespace is not None:
            return namespace
        if self._current_agent_id is not None:
            return f"{AGENT_NAMESPACE_PREFIX}{self._current_agent_id}"
        return None

    # ── CRUD ──────────────────────────────────────────────────────────────

    async def add(
        self,
        content: str,
        title: str | None = None,
        tags: list[str] | None = None,
        file: str | None = None,
        namespace: str | None = None,
        template: str | None = None,
        force_unsafe: bool = False,
    ) -> dict:
        """Add a memory entry. Returns dict with file path and chunk count.

        When an agent session is active (``start_agent_session`` was
        called), ``namespace=None`` defaults to the agent's private
        ``agent-runtime:<id>`` bucket. Pass an explicit ``namespace=`` to
        override (e.g. ``"shared"``).

        Content passes through the trust-boundary redaction guard before
        any filesystem write. On a hit the call returns ``{"error":
        "redaction_blocked", "hits": N}`` instead of writing; pass
        ``force_unsafe=True`` to bypass with audit logging.
        """
        comp = await self._ensure_init()
        from datetime import datetime, timezone

        from memtomem import privacy
        from memtomem.tools.memory_writer import append_entry

        # Apply template
        if template:
            from memtomem.templates import render_template

            content = render_template(template, content, title=title)

        guard = privacy.enforce_write_guard(
            content,
            surface="langgraph_add",
            force_unsafe=force_unsafe,
            audit_context={"namespace": namespace, "file": file},
        )
        if guard.decision == "blocked":
            return {
                "error": "redaction_blocked",
                "hits": len(guard.hits),
                "surface": "langgraph_add",
            }

        if file:
            target = Path(file).expanduser().resolve()
        else:
            from memtomem.errors import ConfigError
            from memtomem.memory_scope import require_user_base

            try:
                base = require_user_base(comp.config.indexing.memory_dirs)
            except ConfigError as exc:
                return {"error": str(exc)}
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            target = base / f"{date_str}.md"

        effective_namespace = self._resolve_add_namespace(namespace)

        append_entry(target, content, title=title, tags=tags)
        # Guarded above (``enforce_write_guard``); skip the engine gate (ADR-0006 PR-A).
        stats = await comp.index_engine.index_file(
            target, namespace=effective_namespace, already_scanned=True
        )

        return {
            "file": str(target),
            "indexed_chunks": stats.indexed_chunks,
        }

    async def get(self, chunk_id: str) -> dict | None:
        """Get a chunk by UUID. Returns dict or None."""
        comp = await self._ensure_init()
        chunk = await comp.storage.get_chunk(UUID(chunk_id))
        if chunk is None:
            return None
        return {
            "id": str(chunk.id),
            "content": chunk.content,
            "source": str(chunk.metadata.source_file),
            "tags": list(chunk.metadata.tags),
            "namespace": chunk.metadata.namespace,
        }

    async def delete(self, chunk_id: str) -> bool:
        """Delete a chunk by UUID."""
        comp = await self._ensure_init()
        deleted = await comp.storage.delete_chunks([UUID(chunk_id)])
        return deleted > 0

    # ── Sessions (Episodic Memory) ────────────────────────────────────────

    async def start_session(self, agent_id: str = "default", namespace: str | None = None) -> str:
        """Start an episodic memory session. Returns session_id.

        Low-level escape hatch — for multi-agent scenarios prefer
        :meth:`start_agent_session`, which derives the namespace from
        ``agent-runtime:<id>`` and binds ``_current_agent_id`` so
        :meth:`search` / :meth:`add` can default to the agent scope.

        ``agent_id`` is **not** run through ``validate_agent_id`` here:
        this method does not concatenate it into ``AGENT_NAMESPACE_PREFIX``,
        so a malformed value cannot produce an ``"agent-runtime:foo:bar"``
        namespace string. The id still lands in the sessions row as
        metadata; downstream code that reads it back must not feed it
        into a namespace concat without validating first. New paths that
        derive a namespace from ``agent_id`` should use
        :meth:`start_agent_session` (or call ``validate_agent_id``
        directly) so the gate isn't reintroduced as a regression.

        ``namespace`` *is* run through :func:`validate_namespace` because
        an explicit override lands verbatim in the session row — without
        the gate a Python caller could write ``"agent-runtime:foo:bar"``
        through this entry point even though the equivalent
        ``start_agent_session`` path now refuses it (issue #496).
        """
        comp = await self._ensure_init()
        if namespace is not None:
            validate_namespace(namespace)
        session_id = str(uuid4())
        ns = namespace or "default"
        await comp.storage.create_session(session_id, agent_id, ns)
        async with self._session_lock:
            self._current_session_id = session_id
            # Agent binding follows the session lifecycle: replacing an
            # agent-bound session with a low-level one must not leave
            # ``add`` / ``search`` defaulting to the previous agent's
            # ``agent-runtime:<id>`` scope while events log to the new
            # session (same reset contract as ``end_session``).
            self._current_agent_id = None
        return session_id

    async def start_agent_session(
        self,
        agent_id: str,
        *,
        namespace: str | None = None,
    ) -> str:
        """Start a multi-agent-aware episodic memory session.

        Derives the namespace from ``agent-runtime:<agent_id>`` (override
        with explicit ``namespace=``), records the session in storage, and
        binds ``_current_agent_id`` so subsequent ``search`` /
        ``add`` calls inherit the agent scope without the caller passing
        ``namespace=`` on every call.

        Passing the reserved ``agent_id="default"`` starts an *unbound*
        session instead: the row namespace stays ``"default"`` and
        ``_current_agent_id`` stays ``None``, so ``add`` / ``search``
        behave as they do with no agent session. Mirrors the MCP
        ``mem_session_start`` surface (#1875); prefer :meth:`start_session`
        when that is what you meant.

        Returns the session id.

        Raises:
            InvalidNameError: ``agent_id`` is empty, contains ``:``, ``/``,
                ``..``, whitespace, control characters, or anything outside
                ``[A-Za-z0-9._-]`` — the same gate the MCP / CLI session
                surfaces apply (see ``memtomem.constants.validate_agent_id``).
                This blocks malformed values from concatenating into
                ``agent-runtime:<agent_id>`` and round-tripping into
                storage as ``"agent-runtime:foo:bar"``.

                Or ``namespace`` is supplied with a malformed value (see
                ``memtomem.constants.validate_namespace``). The override is
                an escape hatch but not a bypass: a Python caller cannot
                land ``"agent-runtime:foo:bar"`` in the session row even
                though ``agent_id`` itself was clean (issue #496 — closes
                the kin gap to the ``agent_id`` work in #486 / #492).
        """
        # Validate-then-normalize: malformed ids still raise, while the
        # reserved "default" collapses to an unbound session (#1875), the
        # same rule the MCP ``mem_session_start`` surface applies. Without
        # it this method would bind ``agent-runtime:default`` and route
        # every subsequent ``add`` into a hidden system namespace.
        #
        # ``required=True``: ``agent_id`` is a mandatory positional here,
        # unlike the MCP surface where omitting it is the documented way to
        # start an unbound session. Without it a ``None`` would read as
        # "nothing to bind" and land in the NOT NULL ``sessions.agent_id``
        # column as a backend IntegrityError instead of the
        # InvalidNameError callers have always gotten.
        bound_agent_id = normalize_bound_agent_id(agent_id, required=True)
        if namespace is not None:
            validate_namespace(namespace)

        comp = await self._ensure_init()
        session_id = str(uuid4())
        if namespace:
            ns = namespace
        elif bound_agent_id:
            ns = f"{AGENT_NAMESPACE_PREFIX}{bound_agent_id}"
        else:
            ns = "default"
        # The row keeps the literal; only the runtime binding is None.
        await comp.storage.create_session(session_id, agent_id, ns)
        async with self._session_lock:
            self._current_session_id = session_id
            self._current_agent_id = bound_agent_id
        return session_id

    async def end_session(self, summary: str | None = None) -> dict:
        """End the current session. Returns session stats.

        Resets both ``_current_session_id`` and ``_current_agent_id``,
        so subsequent ``search(include_shared=True)`` calls without a
        new ``start_agent_session`` will raise.
        """
        comp = await self._ensure_init()
        if not self._current_session_id:
            return {"error": "no active session"}

        events = await comp.storage.get_session_events(self._current_session_id)
        event_counts: dict[str, int] = {}
        for e in events:
            event_counts[e["event_type"]] = event_counts.get(e["event_type"], 0) + 1

        end_metadata: dict = {"event_counts": event_counts}
        # A summary passed here is caller-supplied, not the server's
        # write-provenance selection — recorded as ``manual``. Ending with no
        # summary leaves the origin absent (unknown), never a bare marker.
        if summary:
            from memtomem.server.tools._provenance import SUMMARY_PROVENANCE_MANUAL

            end_metadata["summary_provenance"] = SUMMARY_PROVENANCE_MANUAL
        await comp.storage.end_session(
            self._current_session_id,
            summary,
            end_metadata,
        )
        await comp.storage.scratch_cleanup(session_id=self._current_session_id)

        sid = self._current_session_id
        async with self._session_lock:
            self._current_session_id = None
            self._current_agent_id = None
        return {"session_id": sid, "events": len(events), "event_counts": event_counts}

    async def log_event(
        self, event_type: str, content: str, chunk_ids: list[str] | None = None
    ) -> None:
        """Log an event to the current session."""
        if not self._current_session_id:
            return
        comp = await self._ensure_init()
        await comp.storage.add_session_event(
            self._current_session_id,
            event_type,
            content,
            chunk_ids,
        )

    # ── Working Memory ────────────────────────────────────────────────────

    async def scratch_set(self, key: str, value: str, ttl_minutes: int | None = None) -> None:
        """Store a value in working memory."""
        comp = await self._ensure_init()
        from datetime import datetime, timedelta, timezone

        expires_at = None
        if ttl_minutes:
            expires_at = (datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)).isoformat(
                timespec="seconds"
            )
        await comp.storage.scratch_set(
            key, value, session_id=self._current_session_id, expires_at=expires_at
        )

    async def scratch_get(self, key: str) -> str | None:
        """Get a value from working memory."""
        comp = await self._ensure_init()
        entry = await comp.storage.scratch_get(key)
        return entry["value"] if entry else None

    async def scratch_list(self) -> list[dict]:
        """List all working memory entries."""
        comp = await self._ensure_init()
        return await comp.storage.scratch_list(session_id=self._current_session_id)

    # ── Index ─────────────────────────────────────────────────────────────

    async def index(
        self, path: str = ".", recursive: bool = True, namespace: str | None = None
    ) -> dict:
        """Index files for search."""
        comp = await self._ensure_init()
        stats = await comp.index_engine.index_path(
            Path(path).expanduser().resolve(),
            recursive=recursive,
            namespace=namespace,
        )
        return {
            "total_files": stats.total_files,
            "indexed_chunks": stats.indexed_chunks,
            "duration_ms": stats.duration_ms,
            "blocked_files": stats.blocked_files,
            "blocked_paths": list(stats.blocked_paths),
            "errors": list(stats.errors),
        }

    # ── Context Manager ───────────────────────────────────────────────────

    async def __aenter__(self) -> Self:
        await self._ensure_init()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()
