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
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID, uuid4


class MemtomemStore:
    """LangGraph-compatible memory store wrapping memtomem components.

    Provides a simple async API for search, add, sessions, and working memory.
    Components are lazily initialized on first use.

    Args:
        config_overrides: Optional dict of config overrides (e.g. {"storage": {"sqlite_path": "..."}})
    """

    def __init__(self, config_overrides: dict[str, Any] | None = None):
        self._components = None
        self._config_overrides = config_overrides or {}
        self._current_session_id: str | None = None

    async def _ensure_init(self):
        if self._components is not None:
            return
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

    async def close(self):
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
    ) -> list[dict]:
        """Search indexed memories.

        Returns list of dicts with keys: id, content, score, source, tags, namespace.
        """
        await self._ensure_init()
        rrf_weights = None
        if bm25_weight is not None or dense_weight is not None:
            rrf_weights = [bm25_weight or 1.0, dense_weight or 1.0]

        results, stats = await self._components.search_pipeline.search(
            query=query,
            top_k=top_k,
            namespace=namespace,
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
        """Add a memory entry. Returns dict with file path and chunk count."""
        await self._ensure_init()
        from datetime import datetime, timezone
        from memtomem.tools.memory_writer import append_entry

        comp = self._components

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

        append_entry(target, content, title=title, tags=tags)
        stats = await comp.index_engine.index_file(target, namespace=namespace)

        return {
            "file": str(target),
            "indexed_chunks": stats.indexed_chunks,
        }

    async def get(self, chunk_id: str) -> dict | None:
        """Get a chunk by UUID. Returns dict or None."""
        await self._ensure_init()
        chunk = await self._components.storage.get_chunk(UUID(chunk_id))
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
        await self._ensure_init()
        deleted = await self._components.storage.delete_chunks([UUID(chunk_id)])
        return deleted > 0

    # ── Sessions (Episodic Memory) ────────────────────────────────────────

    async def start_session(self, agent_id: str = "default", namespace: str | None = None) -> str:
        """Start an episodic memory session. Returns session_id."""
        await self._ensure_init()
        session_id = str(uuid4())
        ns = namespace or "default"
        await self._components.storage.create_session(session_id, agent_id, ns)
        self._current_session_id = session_id
        return session_id

    async def end_session(self, summary: str | None = None) -> dict:
        """End the current session. Returns session stats."""
        await self._ensure_init()
        if not self._current_session_id:
            return {"error": "no active session"}

        events = await self._components.storage.get_session_events(self._current_session_id)
        event_counts: dict[str, int] = {}
        for e in events:
            event_counts[e["event_type"]] = event_counts.get(e["event_type"], 0) + 1

        await self._components.storage.end_session(
            self._current_session_id,
            summary,
            {"event_counts": event_counts},
        )
        await self._components.storage.scratch_cleanup(session_id=self._current_session_id)

        sid = self._current_session_id
        self._current_session_id = None
        return {"session_id": sid, "events": len(events), "event_counts": event_counts}

    async def log_event(self, event_type: str, content: str, chunk_ids: list[str] | None = None):
        """Log an event to the current session."""
        if not self._current_session_id:
            return
        await self._ensure_init()
        await self._components.storage.add_session_event(
            self._current_session_id,
            event_type,
            content,
            chunk_ids,
        )

    # ── Working Memory ────────────────────────────────────────────────────

    async def scratch_set(self, key: str, value: str, ttl_minutes: int | None = None):
        """Store a value in working memory."""
        await self._ensure_init()
        from datetime import datetime, timedelta, timezone

        expires_at = None
        if ttl_minutes:
            expires_at = (datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)).isoformat(
                timespec="seconds"
            )
        await self._components.storage.scratch_set(
            key, value, session_id=self._current_session_id, expires_at=expires_at
        )

    async def scratch_get(self, key: str) -> str | None:
        """Get a value from working memory."""
        await self._ensure_init()
        entry = await self._components.storage.scratch_get(key)
        return entry["value"] if entry else None

    async def scratch_list(self) -> list[dict]:
        """List all working memory entries."""
        await self._ensure_init()
        return await self._components.storage.scratch_list(session_id=self._current_session_id)

    # ── Index ─────────────────────────────────────────────────────────────

    async def index(
        self, path: str = ".", recursive: bool = True, namespace: str | None = None
    ) -> dict:
        """Index files for search."""
        await self._ensure_init()
        stats = await self._components.index_engine.index_directory(
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

    async def __aenter__(self):
        await self._ensure_init()
        return self

    async def __aexit__(self, *args):
        await self.close()
