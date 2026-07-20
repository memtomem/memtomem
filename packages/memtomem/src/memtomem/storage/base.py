"""Storage backend protocol."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator, Protocol, Sequence
from uuid import UUID

from memtomem.models import Chunk, ChunkLink, NamespaceFilter, ScopeFilter, SearchResult


@dataclass(frozen=True)
class ChunkAuditRow:
    """Minimal chunk fields needed for a full-scope privacy audit walk.

    Returned by :meth:`StorageBackend.iter_chunks_for_audit`. Holds only
    the fields the rescan command actually reads — ``content`` for the
    guard scan, ``chunk_id`` / ``source`` / ``scope`` / ``project_root``
    for the violation report. Kept distinct from :class:`memtomem.models.Chunk`
    so the audit enumerator can stream rows without paying for the full
    ``_row_to_chunk`` decode (tags JSON, heading hierarchy, embedding
    lookup, etc.).
    """

    chunk_id: str
    source: Path
    content: str
    scope: str
    project_root: Path | None


@dataclass(frozen=True, slots=True)
class NamespaceRenameResult:
    """Outcome of :meth:`StorageBackend.rename_namespace`.

    A bare row count could not express the whole outcome: a namespace
    registered through ``set_namespace_meta`` but never written to has no
    chunks at all, so ``chunks_moved == 0`` while its metadata row *was*
    renamed — "0" never meant "nothing changed". Each flag names one
    thing that happened so callers can phrase the result honestly.
    """

    #: Rows rewritten in ``chunks``. Zero is a legitimate success.
    chunks_moved: int
    #: The source's ``namespace_metadata`` row moved to the new name.
    #: False both when the source had no metadata row and when the row
    #: was dropped in favour of an existing target row (merge).
    metadata_renamed: bool
    #: The target already existed and ``merge=True`` consolidated into it.
    merged: bool
    #: Source chunks the target already held (same file, content hash and
    #: start line) and which were therefore deleted rather than moved —
    #: ``chunks`` is UNIQUE on that key. Only ever non-zero on a merge.
    duplicates_dropped: int = 0


@dataclass(frozen=True, slots=True)
class SearchMetadataFilter:
    """Exact metadata constraints applied before retrieval limits."""

    source_exact: tuple[str, ...] = ()
    chunk_types: tuple[str, ...] = ()
    created_from: datetime | None = None
    created_before: datetime | None = None


class StorageBackend(Protocol):
    async def initialize(self) -> None: ...
    async def close(self) -> None: ...

    # Transaction
    def transaction(self) -> AbstractAsyncContextManager[None]: ...

    # Chunk CRUD
    async def upsert_chunks(self, chunks: Sequence[Chunk]) -> int: ...
    async def update_chunk_line_ranges(self, chunks: Sequence[Chunk]) -> int: ...
    async def get_chunk(self, chunk_id: UUID) -> Chunk | None: ...
    async def get_chunks_batch(self, chunk_ids: Sequence[UUID]) -> dict[UUID, Chunk]: ...
    async def delete_chunks(self, chunk_ids: Sequence[UUID]) -> int: ...
    async def delete_by_source(self, source_file: Path) -> int: ...
    async def list_scopes_by_source(self, source_file: Path) -> set[str]: ...
    async def list_scopes_by_namespace(self, namespace: str) -> set[str]: ...
    async def list_sources_by_namespace(self, namespace: str) -> list[Path]: ...
    async def update_chunks_scope_for_source(
        self,
        old_path: Path,
        new_path: Path,
        new_scope: str,
        new_project_root: Path | None,
    ) -> int: ...

    # Search
    #
    # ADR-0011 §6 always-on scope-context filter: every backend that
    # serves search MUST honour ``scope_filter`` + ``project_context_root``,
    # otherwise the project-aware default merge silently degrades to a
    # cross-project union on alternate backends. Both kwargs default to
    # ``None`` so existing callers stay source-compatible; the
    # ``scope_context_sql`` helper in :mod:`memtomem.storage.sqlite_scope`
    # reflects how a SQL backend should compose the fragment.
    async def bm25_search(
        self,
        query: str,
        top_k: int = 20,
        namespace_filter: NamespaceFilter | None = None,
        scope_filter: ScopeFilter | None = None,
        project_context_root: Path | None = None,
        metadata_filter: SearchMetadataFilter | None = None,
    ) -> list[SearchResult]: ...
    async def dense_search(
        self,
        embedding: list[float],
        top_k: int = 20,
        namespace_filter: NamespaceFilter | None = None,
        scope_filter: ScopeFilter | None = None,
        project_context_root: Path | None = None,
        metadata_filter: SearchMetadataFilter | None = None,
        *,
        exhaustive: bool = False,
    ) -> list[SearchResult]: ...

    # Metadata
    async def get_chunk_hashes(self, source_file: Path) -> dict[str, str]: ...
    async def get_chunk_index_state(
        self, source_file: Path
    ) -> dict[str, tuple[str, tuple[str, ...]]]: ...
    async def get_chunk_ids_by_hashes(self, content_hashes: Sequence[str]) -> dict[str, UUID]: ...
    async def get_stats(self) -> dict[str, int]: ...
    async def get_all_source_files(self) -> set[Path]: ...
    async def search_source_files_by_content(
        self, query: str, limit: int = 10000
    ) -> list[Path]: ...
    async def list_chunks_by_source(self, source_file: Path, limit: int = 50) -> list[Chunk]: ...
    async def count_chunks_by_source(self, source_file: Path) -> int: ...
    async def count_chunk_links_for_source(self, source_file: Path) -> int: ...
    async def list_chunks_by_sources(
        self,
        source_files: Sequence[Path],
        limit_per_file: int = 10000,
    ) -> dict[Path, list[Chunk]]: ...
    async def recall_chunks(
        self,
        since: datetime | None = None,
        until: datetime | None = None,
        source_filter: str | None = None,
        limit: int = 20,
        namespace_filter: NamespaceFilter | None = None,
        tag_filter: str | None = None,
        scope_filter: ScopeFilter | None = None,
        project_context_root: Path | None = None,
        metadata_filter: SearchMetadataFilter | None = None,
    ) -> list[Chunk]: ...

    # Audit enumeration (independent of search / recall paths)
    #
    # ``recall_chunks`` is the UI/CLI helper — it caps at ``limit=20`` and its
    # ordering is tuned for "show me the most recent / relevant N". A full-
    # scope privacy audit (``mm mem rescan``) needs every chunk in the scope
    # streamed in a stable, paginated order with no UI-side limit. Adding a
    # separate method keeps the audit-only contract (no embedding lookup, no
    # tag decode, no per-row search post-processing) from drifting into the
    # search path. Source filtering uses exact-match + descendant-prefix only;
    # no fuzzy / substring matching.
    def iter_chunks_for_audit(
        self,
        *,
        scope: str,
        source_exact: Path | None = None,
        source_prefix: Path | None = None,
        batch_size: int = 500,
    ) -> AsyncIterator[ChunkAuditRow]: ...

    # Namespace
    async def list_namespaces(self) -> list[tuple[str, int]]: ...
    async def count_chunks_by_ns_prefix(self, prefixes: Sequence[str]) -> int: ...
    async def delete_by_namespace(self, namespace: str) -> int: ...
    async def rename_namespace(
        self, old: str, new: str, *, merge: bool = False
    ) -> NamespaceRenameResult: ...

    # Tags
    async def get_tag_counts(self) -> list[tuple[str, int]]: ...
    async def list_chunks_by_tag(self, tag: str, limit: int = 10) -> list[Chunk]: ...
    async def count_chunks_by_tag(self, tag: str) -> int: ...
    async def count_chunks_by_any_tag(self, tags: Sequence[str]) -> int: ...
    async def rename_tag(self, old_tag: str, new_tag: str) -> int: ...
    async def delete_tag(self, tag: str) -> int: ...
    async def merge_tags(self, sources: Sequence[str], target: str) -> int: ...

    # Access tracking
    async def increment_access(self, chunk_ids: Sequence[UUID]) -> None: ...
    async def get_access_counts(self, chunk_ids: Sequence[UUID]) -> dict[str, int]: ...

    # Cross-references
    async def add_relation(
        self, source_id: UUID, target_id: UUID, relation_type: str = "related"
    ) -> None: ...
    async def get_related(self, chunk_id: UUID) -> list[tuple[UUID, str]]: ...
    async def delete_relation(self, source_id: UUID, target_id: UUID) -> bool: ...

    # Share lineage (chunk_links)
    async def add_chunk_link(
        self,
        source_id: UUID | None,
        target_id: UUID,
        link_type: str,
        namespace_target: str,
    ) -> None: ...
    async def get_chunk_link(
        self, target_id: UUID, link_type: str = "shared"
    ) -> ChunkLink | None: ...
    async def get_chunks_shared_from(
        self, source_id: UUID, link_type: str | None = None
    ) -> list[ChunkLink]: ...
    async def walk_share_chain(
        self,
        target_id: UUID,
        *,
        link_type: str = "shared",
        max_depth: int = 100,
    ) -> list[ChunkLink]: ...

    # Sessions (episodic memory)
    async def create_session(
        self, session_id: str, agent_id: str, namespace: str, metadata: dict | None = None
    ) -> None: ...
    async def end_session(self, session_id: str, summary: str | None, metadata: dict) -> None: ...
    async def add_session_event(
        self,
        session_id: str,
        event_type: str,
        content: str,
        chunk_ids: list[str] | None = None,
        metadata: dict | None = None,
    ) -> None: ...
    async def list_sessions(
        self, agent_id: str | None = None, since: str | None = None, limit: int = 20
    ) -> list[dict]: ...
    async def get_session(self, session_id: str) -> dict | None: ...
    async def find_stale_active_sessions(
        self, started_before: str, *, limit: int = 100
    ) -> list[dict]: ...
    async def get_session_events(self, session_id: str) -> list[dict]: ...

    # Working memory (scratchpad)
    async def scratch_set(
        self, key: str, value: str, session_id: str | None = None, expires_at: str | None = None
    ) -> None: ...
    async def scratch_get(self, key: str) -> dict | None: ...
    async def scratch_list(self, session_id: str | None = None) -> list[dict]: ...
    async def scratch_delete(self, key: str) -> bool: ...
    async def scratch_cleanup(self, session_id: str | None = None) -> int: ...

    # Idempotency ledger (issue #1573)
    async def idempotency_get(self, tool: str, key: str) -> str | None: ...
    async def idempotency_claim(
        self, tool: str, key: str, ttl_s: int = 86_400
    ) -> tuple[str, str | None]: ...
    async def idempotency_complete(
        self, tool: str, key: str, result: str, ttl_s: int = 86_400
    ) -> None: ...
    async def idempotency_release(self, tool: str, key: str) -> None: ...

    # Search history
    async def save_query_history(
        self,
        query_text: str,
        query_embedding: list[float],
        result_chunk_ids: list[str],
        result_scores: list[float],
    ) -> None: ...
    async def get_query_history(self, limit: int = 20, since: str | None = None) -> list[dict]: ...
    async def suggest_queries(self, prefix: str, limit: int = 5) -> list[str]: ...

    # Explicit relevance feedback on observed search runs (#1801)
    async def save_search_feedback(
        self, run_id: str, chunk_id: str, judgment: str, *, replace: bool = False
    ) -> dict: ...
    async def get_search_feedback(self, run_id: str) -> list[dict]: ...
    async def get_search_run(self, run_id: str) -> dict: ...
    async def get_search_runs(self, limit: int = 50, since: str | None = None) -> list[dict]: ...

    # Importance scoring
    async def update_importance_scores(self, scores: dict[str, float]) -> int: ...
    async def get_importance_scores(self, chunk_ids: list) -> dict[str, float]: ...

    # Analytics (replaces direct _get_db() access in tools)
    async def get_health_report(self, namespace: str | None = None) -> dict: ...
    async def get_frequently_accessed(
        self, namespace: str | None = None, limit: int = 20
    ) -> list[dict]: ...
    async def get_agent_sessions(self, since: str | None = None, limit: int = 20) -> list[dict]: ...
    async def get_knowledge_gaps(self, limit: int = 10) -> list[dict]: ...
    async def get_most_connected(self, limit: int = 5) -> list[dict]: ...
    async def get_chunk_factors(self, namespace: str | None = None) -> list[dict]: ...
    async def get_consolidation_groups(
        self, min_size: int = 3, max_groups: int = 10
    ) -> list[dict]: ...

    # Scratch promote
    async def scratch_promote(self, key: str) -> bool: ...

    # Namespace metadata
    async def get_namespace_meta(self, namespace: str) -> dict | None: ...
    async def set_namespace_meta(
        self, namespace: str, description: str | None = None, color: str | None = None
    ) -> None: ...
    async def list_namespace_meta(self) -> list[dict]: ...
    async def assign_namespace(
        self,
        namespace: str,
        source_filter: str | None = None,
        old_namespace: str | None = None,
    ) -> int: ...
