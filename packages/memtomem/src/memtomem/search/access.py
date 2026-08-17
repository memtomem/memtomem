"""Access-frequency scoring — boost chunks that are searched/used more often."""

from __future__ import annotations

import math

from memtomem.models import SearchResult


def boosted_score(score: float, factor: float) -> float:
    """Apply a ``>= 1.0`` boost factor so the score always moves up.

    Shared by every modifier stage (access, importance, entity-match). Plain
    multiplication only promotes on a non-negative scale: cross-encoder rerank
    scores are raw logits and are routinely negative, where ``-0.3 * 1.5``
    is ``-0.45`` — the boosted chunk sinks below an unboosted ``-0.9``. On a
    negative score the equivalent move toward zero is division, which keeps the
    transform monotonic in ``factor``, continuous at zero, and identical to the
    historical behavior everywhere scores are non-negative (BM25, dense, RRF,
    and rerank providers that emit [0, 1] relevance).
    """
    if score < 0:
        return score / factor if factor else score
    return score * factor


def access_boost(access_count: int, max_boost: float = 1.5) -> float:
    """Log-scale boost based on access count.

    access_count=0  → 1.0  (no boost)
    access_count=10 → ~1.3
    access_count=100 → ~1.5 (max_boost)
    """
    if access_count <= 0:
        return 1.0
    return 1.0 + (max_boost - 1.0) * math.log1p(access_count) / math.log1p(100)


def apply_access_boost(
    results: list[SearchResult],
    access_counts: dict[str, int],
    max_boost: float = 1.5,
) -> list[SearchResult]:
    """Apply access-frequency boost to search results and re-sort.

    Args:
        results: Search results to boost.
        access_counts: Mapping of chunk_id (str) → access_count.
        max_boost: Maximum multiplier for highly accessed chunks.

    Returns:
        Re-sorted results with boosted scores.
    """
    boosted = []
    for r in results:
        count = access_counts.get(str(r.chunk.id), 0)
        factor = access_boost(count, max_boost)
        boosted.append(
            SearchResult(
                chunk=r.chunk,
                score=boosted_score(r.score, factor),
                rank=r.rank,
                source=r.source,
            )
        )

    boosted.sort(key=lambda r: r.score, reverse=True)

    return [
        SearchResult(chunk=r.chunk, score=r.score, rank=i + 1, source=r.source)
        for i, r in enumerate(boosted)
    ]
