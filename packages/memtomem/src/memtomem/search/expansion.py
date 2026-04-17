"""Query expansion — enrich search queries with related terms."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from memtomem.embedding.base import EmbeddingProvider
    from memtomem.llm.base import LLMProvider
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
        logger.warning("Tag expansion failed; returning original query", exc_info=True)
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
        logger.warning("Heading expansion failed; returning original query", exc_info=True)
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


# ---------------------------------------------------------------------------
# LLM-based expansion
# ---------------------------------------------------------------------------

_EXPANSION_SYSTEM_PROMPT = (
    "You are a search query expansion assistant. Given a user's search "
    "query, generate synonyms and closely related terms that would help "
    "retrieve relevant documents. Output ONLY a comma-separated list of "
    "terms, nothing else. Do not repeat words already in the query."
)

# Hard timeout to prevent search latency blow-up (seconds).
_LLM_EXPANSION_TIMEOUT = 3.0


async def expand_query_llm(
    query: str,
    llm_provider: LLMProvider,
    max_terms: int = 3,
) -> str:
    """Expand query using an LLM to generate synonyms / related terms.

    A hard timeout of 3 seconds prevents search from hanging when the
    LLM provider is slow. On timeout the original query is returned
    (handled by the caller).
    """
    from memtomem.llm.utils import strip_llm_response

    prompt = f'Expand this search query with up to {max_terms} related terms:\n"{query}"'

    raw = await asyncio.wait_for(
        llm_provider.generate(prompt, system=_EXPANSION_SYSTEM_PROMPT, max_tokens=256),
        timeout=_LLM_EXPANSION_TIMEOUT,
    )
    cleaned = strip_llm_response(raw)

    query_words = set(query.lower().split())
    terms: list[str] = []
    for part in cleaned.split(","):
        term = part.strip()
        if term and term.lower() not in query_words:
            terms.append(term)
    if not terms:
        return query
    expanded = query + " " + " ".join(terms[:max_terms])
    logger.debug("Query expanded (llm): %r -> %r", query, expanded)
    return expanded
