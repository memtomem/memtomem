"""Core data models for memtomem."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from uuid import UUID, uuid4


class ChunkType(StrEnum):
    MARKDOWN_SECTION = "markdown_section"
    PYTHON_FUNCTION = "python_function"
    PYTHON_CLASS = "python_class"
    JS_FUNCTION = "js_function"
    RST_SECTION = "rst_section"
    RAW_TEXT = "raw_text"
    PROCEDURE = "procedure"


@dataclass(frozen=True, slots=True)
class ChunkMetadata:
    source_file: Path
    heading_hierarchy: tuple[str, ...] = ()
    chunk_type: ChunkType = ChunkType.RAW_TEXT
    start_line: int = 0
    end_line: int = 0
    language: str = "en"
    tags: tuple[str, ...] = ()
    namespace: str = "default"
    overlap_before: int = 0  # chars of overlap with previous chunk
    overlap_after: int = 0  # chars of overlap with next chunk
    parent_context: str = ""  # parent heading or document title
    file_context: str = ""  # filename + heading outline
    # Validity window from frontmatter (RFC: temporal-validity).
    # NULL means unbounded on that side; both NULL means always-valid.
    valid_from_unix: int | None = None
    valid_to_unix: int | None = None
    # Scope axis (ADR-0011). ``user`` is the only scope today; project_shared
    # / project_local are reserved for the read/write surface PRs.
    # ``project_root`` is None for user scope, an absolute path for project
    # scopes — required so a single user-local DB can hold chunks from
    # multiple worktrees of the same project without path-prefix collisions.
    scope: str = "user"
    project_root: Path | None = None


@dataclass(frozen=True, slots=True)
class NamespaceFilter:
    """Filter for namespace-scoped queries.

    Supports exact match (single or union), glob patterns, comma-separated
    lists, and default-search exclusion of system namespace prefixes (e.g.
    ``archive:``). Exclusion is applied *only* when no explicit namespace
    is given — the idea is that callers who ask for ``archive:summary``
    directly have already opted in.
    """

    namespaces: tuple[str, ...] = ()
    pattern: str | None = None
    exclude_prefixes: tuple[str, ...] = ()

    @staticmethod
    def parse(
        value: str | list[str] | None,
        system_prefixes: tuple[str, ...] | list[str] | None = None,
    ) -> NamespaceFilter | None:
        """Parse a user-supplied namespace argument into a filter.

        When ``value`` is ``None`` and ``system_prefixes`` is non-empty, the
        returned filter carries ``exclude_prefixes`` so default searches
        hide system-generated namespaces (``archive:*`` by default) without
        affecting explicit queries. When ``value`` is any non-``None`` form
        (exact string, comma list, glob), ``system_prefixes`` is ignored —
        the caller explicitly opted into whatever namespace they named.
        """
        prefixes = tuple(system_prefixes) if system_prefixes else ()

        if value is None:
            if prefixes:
                return NamespaceFilter(exclude_prefixes=prefixes)
            return None
        if isinstance(value, list):
            return NamespaceFilter(namespaces=tuple(value))
        if "*" in value:
            return NamespaceFilter(pattern=value)
        if "," in value:
            return NamespaceFilter(namespaces=tuple(v.strip() for v in value.split(",")))
        return NamespaceFilter(namespaces=(value,))


@dataclass(frozen=True, slots=True)
class ScopeFilter:
    """Filter for scope-axis (ADR-0011) queries.

    Sibling of :class:`NamespaceFilter`. Supports exact match (single or
    union) and glob patterns over the three scope values
    (``user`` / ``project_shared`` / ``project_local``). Comma-separated
    lists are normalised to the union form.

    The "context boundary" — the always-on rule that out-of-project
    searches see only ``user`` and in-project searches see ``user`` plus
    the current project's project-tier rows — lives in the SQL helper
    (:func:`memtomem.storage.sqlite_scope.scope_context_sql`), not in
    the filter itself. This keeps the filter a pure user-intent value;
    the helper is the place where caller intent meets project context.
    """

    scopes: tuple[str, ...] = ()
    pattern: str | None = None

    @staticmethod
    def parse(value: str | list[str] | None) -> ScopeFilter | None:
        """Parse a user-supplied scope argument into a filter.

        ``None`` returns ``None`` (caller falls back to the always-on
        context-boundary rule). ``"project_*"`` parses as a glob. Comma
        list and bare exact match work the same as for namespaces. The
        scope alphabet is small (3 values), so this parser deliberately
        does NOT validate against ``user`` / ``project_shared`` /
        ``project_local`` — invalid scope strings produce an empty
        result set rather than an error, mirroring the namespace parser
        which also accepts arbitrary strings.
        """
        if value is None:
            return None
        if isinstance(value, list):
            return ScopeFilter(scopes=tuple(value))
        if "*" in value:
            return ScopeFilter(pattern=value)
        if "," in value:
            return ScopeFilter(scopes=tuple(v.strip() for v in value.split(",")))
        return ScopeFilter(scopes=(value,))


@dataclass(slots=True)
class Chunk:
    content: str
    metadata: ChunkMetadata
    id: UUID = field(default_factory=uuid4)
    content_hash: str = ""
    embedding: list[float] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        if not self.content_hash:
            import unicodedata

            self.content_hash = hashlib.sha256(
                unicodedata.normalize("NFC", self.content).encode()
            ).hexdigest()

    @property
    def retrieval_content(self) -> str:
        """Content with heading hierarchy prefix for embedding and BM25.

        chunk.content stores the pure text (no hierarchy prefix).
        This property prepends the hierarchy for retrieval quality.
        """
        h = self.metadata.heading_hierarchy
        if not h:
            return self.content
        prefix = " > ".join(h)
        return f"{prefix}\n\n{self.content}"


@dataclass(frozen=True, slots=True)
class ContextInfo:
    """Contextual information for a search result chunk."""

    window_before: tuple[Chunk, ...] = ()
    window_after: tuple[Chunk, ...] = ()
    parent_content: str | None = None
    parent_heading: str | None = None
    sibling_count: int = 0
    chunk_position: int = 0  # 1-indexed
    total_chunks_in_file: int = 0
    context_tier_used: str | None = None  # "full" | "standard" | "minimal" | None
    ranked_siblings: tuple[object, ...] = ()  # RankedSibling instances (Feature C)
    related_chunks: tuple[object, ...] = ()  # cross-source related chunks (Feature I)


@dataclass(frozen=True, slots=True)
class SearchResult:
    chunk: Chunk
    score: float
    rank: int
    source: str  # "bm25", "dense", "fused", "reranked", "session_rescue"
    context: ContextInfo | None = None
    # True when this chunk surfaced (at least in part) via the
    # Stage-1 session-summary rescue leg — i.e. an
    # ``archive:session:*`` summary above threshold pointed at this
    # chunk's source file. Preserved by fusion / dedup / decay / MMR /
    # access / importance via OR semantics: if any leg of fusion (or
    # any pre-stage candidate) carried the flag, the merged result
    # carries it too. Surfaced in ``output_format="structured"``
    # payloads only — irrelevant to compact/verbose text formats.
    via_session_summary: bool = False


@dataclass(frozen=True, slots=True)
class ChunkLink:
    """Structured provenance link between two chunks.

    Mirrors a row of the ``chunk_links`` SQL table (see
    ``planning/mem-agent-share-chunk-links-rfc.md``). ``source_id`` is
    ``None`` when the source chunk has been deleted — the FK is
    ``ON DELETE SET NULL``, so the destination chunk and the link row
    survive, but the structured pointer back to the source is gone.
    """

    target_id: UUID
    link_type: str
    namespace_target: str
    created_at: datetime
    source_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class IndexingStats:
    total_files: int
    total_chunks: int
    indexed_chunks: int
    skipped_chunks: int
    deleted_chunks: int
    duration_ms: float
    errors: tuple[str, ...] = ()
    # IDs of chunks actually upserted during this run. Empty when nothing new
    # was written (all candidates were unchanged) or on the zero-result paths
    # (missing file, too large, binary, etc). Consumers that need to act on
    # freshly created chunks — e.g. ``mem_consolidate_apply`` linking a new
    # summary — should read this instead of polling ``recall_chunks``.
    new_chunk_ids: tuple[UUID, ...] = ()
    # Distinct namespaces ``IndexEngine._resolve_namespace`` returned across
    # the indexed file set, in stable (sort) order. ``None`` represents the
    # ``default_namespace == "default"`` carve-out (untagged chunks). Empty
    # on zero-result paths. Surfaced in the web ``IndexResponse`` so the UI
    # can echo what was *actually* applied — important when policy rules
    # split a folder into multiple namespaces.
    resolved_namespaces: tuple[str | None, ...] = ()
    # ADR-0006 PR-A: files skipped by the secret-redaction gate during
    # un-adjudicated bulk indexing (each such file raised ``PrivacyRejection``,
    # was recorded here, and the run continued). ``blocked_files`` is the count;
    # ``blocked_paths`` are the absolute paths so surfaces can list them.
    blocked_files: int = 0
    blocked_paths: tuple[str, ...] = ()
    # Subset of ``blocked_files`` whose scope is ``project_shared`` — those are
    # hard-refused even with ``force_unsafe`` (ADR-0011 §5), so surfaces must
    # not tell the user to retry with ``--force-unsafe`` for them.
    blocked_project_shared_files: int = 0
