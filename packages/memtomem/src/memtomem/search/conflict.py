"""Conflict detection — find contradictions between new and existing memories."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from memtomem.embedding.base import EmbeddingProvider
    from memtomem.models import Chunk
    from memtomem.storage.base import StorageBackend

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ConflictCandidate:
    """A potential conflict between new content and an existing chunk."""

    existing_chunk: Chunk
    similarity: float  # embedding cosine similarity (high)
    text_overlap: float  # Jaccard token overlap (low = conflict)
    conflict_score: float  # similarity - text_overlap


def _jaccard_tokens(a: str, b: str) -> float:
    """Compute Jaccard similarity between token sets."""
    tokens_a = set(a.lower().split())
    tokens_b = set(b.lower().split())
    if not tokens_a or not tokens_b:
        return 0.0
    intersection = tokens_a & tokens_b
    union = tokens_a | tokens_b
    return len(intersection) / len(union)


async def detect_conflicts(
    content: str,
    storage: StorageBackend,
    embedder: EmbeddingProvider,
    threshold: float = 0.75,
    max_candidates: int = 5,
) -> list[ConflictCandidate]:
    """Find existing chunks that semantically match but textually differ.

    A conflict is: high embedding similarity + low text overlap.
    This suggests the same topic is discussed but with different content.

    Args:
        content: New content to check.
        storage: Storage backend.
        embedder: Embedding provider.
        threshold: Minimum similarity to consider.
        max_candidates: Maximum conflicts to return.

    Returns:
        List of conflict candidates sorted by conflict_score descending.
    """
    try:
        embedding = await embedder.embed_query(content)
        results = await storage.dense_search(embedding, top_k=10)
    except Exception as exc:
        logger.debug("Conflict detection failed: %s", exc)
        return []

    candidates: list[ConflictCandidate] = []
    for r in results:
        if r.score < threshold:
            continue

        overlap = _jaccard_tokens(content, r.chunk.content)

        # High similarity + low text overlap = likely conflict
        if overlap < 0.3:
            conflict_score = r.score - overlap
            candidates.append(
                ConflictCandidate(
                    existing_chunk=r.chunk,
                    similarity=r.score,
                    text_overlap=overlap,
                    conflict_score=conflict_score,
                )
            )

    candidates.sort(key=lambda c: c.conflict_score, reverse=True)
    return candidates[:max_candidates]
