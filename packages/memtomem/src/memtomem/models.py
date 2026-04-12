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
    source: str  # "bm25", "dense", "fused", "reranked"
    context: ContextInfo | None = None


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
