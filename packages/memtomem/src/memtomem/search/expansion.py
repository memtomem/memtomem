"""Query expansion — enrich search queries with related terms."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from memtomem.embedding.base import EmbeddingProvider
    from memtomem.storage.base import StorageBackend

logger = logging.getLogger(__name__)


async def expand_query_tags(
    query: str,
    storage: StorageBackend,
    max_terms: int = 3,
) -> str:
    """Expand query by appending matching tag names."""
    try:
        tag_counts = await storage.get_tag_counts()
    except Exception:
        return query

    query_lower = query.lower()
    matched = []
    for tag, _count in tag_counts:
        tag_lower = tag.lower()
        if tag_lower in query_lower or query_lower in tag_lower:
            continue  # already in query
        # Check if any query word is a substring of the tag or vice versa
        words = query_lower.split()
        if any(w in tag_lower or tag_lower in w for w in words if len(w) >= 3):
            matched.append(tag)
    if matched:
        expanded = query + " " + " ".join(matched[:max_terms])
        logger.debug("Query expanded: %r -> %r", query, expanded)
        return expanded
    return query


async def expand_query_headings(
    query: str,
    storage: StorageBackend,
    embedder: EmbeddingProvider,
    max_terms: int = 3,
) -> str:
    """Expand query by appending related heading terms from dense search."""
    try:
        embedding = await embedder.embed_query(query)
        results = await storage.dense_search(embedding, top_k=3)
    except Exception:
        return query

    terms: list[str] = []
    seen = set(query.lower().split())
    for r in results:
        for heading in r.chunk.metadata.heading_hierarchy:
            # Strip markdown heading markers
            text = heading.lstrip("#").strip()
            for word in text.split():
                w = word.lower().strip(".,;:!?")
                if len(w) >= 3 and w not in seen:
                    terms.append(w)
                    seen.add(w)
    if terms:
        expanded = query + " " + " ".join(terms[:max_terms])
        logger.debug("Query expanded (headings): %r -> %r", query, expanded)
        return expanded
    return query
