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

This adapter intentionally does **not** implement LangGraph's
``BaseStore`` (``aput`` / ``aget`` / ``alist_namespaces``). Adding a
full ``BaseStore`` with the same multi-agent awareness is tracked as a
follow-up; for now the multi-agent helpers live on
``start_agent_session`` and ``search(include_shared=...)`` only.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self
from uuid import UUID, uuid4

from memtomem.constants import (
    AGENT_NAMESPACE_PREFIX,
    SHARED_NAMESPACE,
    validate_agent_id,
    validate_namespace,
)

if TYPE_CHECKING:
    from memtomem.server.component_factory import Components


class MemtomemStore:
    """LangGraph-compatible memory store wrapping memtomem components.

    Provides a simple async API for search, add, sessions, and working memory.
    Components are lazily initialized on first use.

    Args:
        config_overrides: Optional dict of config overrides (e.g. {"storage": {"sqlite_path": "..."}})
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
            from memtomem.config import Mem2MemConfig
            from memtomem.server.component_factory import create_components

            config = Mem2MemConfig()

            # Apply overrides
            for section, updates in self._config_overrides.items():
                section_obj = getattr(config, section, None)
                if section_obj and isinstance(updates, dict):
                    for key, value in updates.items():
                        if hasattr(section_obj, key):
                            setattr(section_obj, key, value)

            self._components = await create_components(config)
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

        results, stats = await comp.search_pipeline.search(
            query=query,
            top_k=top_k,
            namespace=effective_namespace,
            source_filter=source_filter,
            tag_filter=tag_filter,
            rrf_weights=rrf_weights,
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
        of that field and runs ``validate_agent_id`` before binding, so any
        value that reaches this point is already gate-checked.
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
        ``start_agent_session`` is the only writer and runs ``validate_agent_id``
        before binding (same invariant as ``_resolve_search_namespace``).
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
    ) -> dict:
        """Add a memory entry. Returns dict with file path and chunk count.

        When an agent session is active (``start_agent_session`` was
        called), ``namespace=None`` defaults to the agent's private
        ``agent-runtime:<id>`` bucket. Pass an explicit ``namespace=`` to
        override (e.g. ``"shared"``).
        """
        comp = await self._ensure_init()
        from datetime import datetime, timezone
        from memtomem.tools.memory_writer import append_entry

        # Apply template
        if template:
            from memtomem.templates import render_template

            content = render_template(template, content, title=title)

        if file:
            target = Path(file).expanduser().resolve()
        else:
            if not comp.config.indexing.memory_dirs:
                return {"error": "No memory directories configured. Run 'mm init' first."}
            base = Path(comp.config.indexing.memory_dirs[0]).expanduser().resolve()
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            target = base / f"{date_str}.md"

        effective_namespace = self._resolve_add_namespace(namespace)

        append_entry(target, content, title=title, tags=tags)
        stats = await comp.index_engine.index_file(target, namespace=effective_namespace)

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
        validate_agent_id(agent_id)
        if namespace is not None:
            validate_namespace(namespace)

        comp = await self._ensure_init()
        session_id = str(uuid4())
        ns = namespace or f"{AGENT_NAMESPACE_PREFIX}{agent_id}"
        await comp.storage.create_session(session_id, agent_id, ns)
        async with self._session_lock:
            self._current_session_id = session_id
            self._current_agent_id = agent_id
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

        await comp.storage.end_session(
            self._current_session_id,
            summary,
            {"event_counts": event_counts},
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
        }

    # ── Context Manager ───────────────────────────────────────────────────

    async def __aenter__(self) -> Self:
        await self._ensure_init()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()
