"""Automatic keyword-based tag extraction for memory chunks."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


# ---------------------------------------------------------------------------
# Stop word sets
# ---------------------------------------------------------------------------

_EN_STOP_WORDS: frozenset[str] = frozenset(
    {
        "the",
        "and",
        "for",
        "are",
        "was",
        "were",
        "been",
        "being",
        "have",
        "has",
        "had",
        "does",
        "did",
        "will",
        "would",
        "could",
        "should",
        "may",
        "might",
        "shall",
        "can",
        "not",
        "but",
        "from",
        "with",
        "this",
        "that",
        "these",
        "those",
        "they",
        "them",
        "their",
        "what",
        "which",
        "who",
        "how",
        "when",
        "where",
        "why",
        "all",
        "any",
        "each",
        "both",
        "few",
        "more",
        "most",
        "other",
        "some",
        "such",
        "than",
        "too",
        "very",
        "just",
        "also",
        "about",
        "into",
        "over",
        "after",
        "here",
        "there",
        "then",
        "use",
        "used",
        "using",
        "its",
        "our",
        "you",
        "your",
    }
)

_KO_STOP_WORDS: frozenset[str] = frozenset(
    {
        "있는",
        "있다",
        "없다",
        "이다",
        "하다",
        "되다",
        "것이다",
        "이런",
        "저런",
        "그런",
        "그리고",
        "하지만",
        "그러나",
        "때문에",
        "위해서",
        "통해서",
        "에서",
        "에게",
        "으로",
        "부터",
        "까지",
        "에는",
        "에도",
        "하는",
        "하고",
        "이것",
        "그것",
        "저것",
        "우리",
        "여기",
        "거기",
        "저기",
    }
)

_STOP_WORDS: frozenset[str] = _EN_STOP_WORDS | _KO_STOP_WORDS

# Matches words with 3+ total chars: starts with letter (ASCII or Korean),
# followed by 2+ alphanumeric / underscore / hyphen chars.
_WORD_RE = re.compile(r"[a-z가-힣][a-z가-힣0-9_-]{2,}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract_tags_keyword(
    text: str,
    max_tags: int = 5,
    *,
    heading_hierarchy: tuple[str, ...] = (),
) -> list[str]:
    """Extract keyword tags from text using word frequency analysis.

    Words from *heading_hierarchy* receive a 3x frequency boost so that
    structural context influences tag ranking.

    Args:
        text: Content to extract tags from.
        max_tags: Maximum number of tags to return (default 5).
        heading_hierarchy: Heading breadcrumbs; weighted higher.

    Returns:
        List of lowercase tag strings sorted by relevance, up to *max_tags*.
    """
    if not text.strip():
        return []

    lowered = text.lower()
    counter: Counter[str] = Counter()

    for word in _WORD_RE.findall(lowered):
        if word not in _STOP_WORDS:
            counter[word] += 1

    # Boost words appearing in the heading hierarchy
    for heading in heading_hierarchy:
        for word in _WORD_RE.findall(heading.lower()):
            if word not in _STOP_WORDS:
                counter[word] += 3  # 3x boost

    return [word for word, _ in counter.most_common(max_tags)]


@dataclass(frozen=True)
class AutoTagStats:
    """Statistics returned by auto_tag_storage."""

    total_chunks: int
    tagged_chunks: int
    skipped_chunks: int


async def auto_tag_storage(
    storage: object,
    source_filter: str | None = None,
    max_tags: int = 5,
    overwrite: bool = False,
    dry_run: bool = False,
) -> AutoTagStats:
    """Apply keyword-based tags to chunks in storage.

    Iterates all source files (optionally filtered), extracts tags from each
    chunk's content, and upserts the updated chunks back to storage.

    Args:
        storage: StorageBackend instance.
        source_filter: Only process sources whose path contains this substring.
        max_tags: Maximum tags to extract per chunk (default 5).
        overwrite: If False (default), skip chunks that already have tags.
        dry_run: If True, compute tags but do NOT write to storage.

    Returns:
        AutoTagStats with total_chunks, tagged_chunks, skipped_chunks counts.
    """
    from memtomem.models import Chunk, ChunkMetadata

    sources = await storage.get_all_source_files()  # type: ignore[union-attr]
    if source_filter:
        sources = {s for s in sources if source_filter in str(s)}

    total = 0
    tagged = 0
    skipped = 0

    for source in sorted(sources):
        chunks: list[Chunk] = await storage.list_chunks_by_source(  # type: ignore[union-attr]
            source, limit=10_000
        )
        for chunk in chunks:
            total += 1

            # Skip chunks that already have tags unless overwrite is requested
            if chunk.metadata.tags and not overwrite:
                skipped += 1
                continue

            new_tags = extract_tags_keyword(
                chunk.content,
                max_tags=max_tags,
                heading_hierarchy=chunk.metadata.heading_hierarchy,
            )
            if not new_tags:
                skipped += 1
                continue

            if not dry_run:
                new_meta = ChunkMetadata(
                    source_file=chunk.metadata.source_file,
                    heading_hierarchy=chunk.metadata.heading_hierarchy,
                    chunk_type=chunk.metadata.chunk_type,
                    start_line=chunk.metadata.start_line,
                    end_line=chunk.metadata.end_line,
                    language=chunk.metadata.language,
                    tags=tuple(new_tags),
                    namespace=chunk.metadata.namespace,
                )
                updated = Chunk(
                    content=chunk.content,
                    metadata=new_meta,
                    id=chunk.id,
                    content_hash=chunk.content_hash,
                    embedding=chunk.embedding,
                    created_at=chunk.created_at,
                    updated_at=datetime.now(timezone.utc),
                )
                await storage.upsert_chunks([updated])  # type: ignore[union-attr]

            tagged += 1

    return AutoTagStats(
        total_chunks=total,
        tagged_chunks=tagged,
        skipped_chunks=skipped,
    )
