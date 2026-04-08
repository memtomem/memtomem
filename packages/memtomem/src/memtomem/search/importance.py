"""Memory importance scoring — composite multi-factor scoring."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from memtomem.models import SearchResult


def compute_importance(
    access_count: int,
    tag_count: int,
    relation_count: int,
    age_days: float,
    weights: tuple[float, ...] = (0.3, 0.2, 0.3, 0.2),
) -> float:
    """Compute composite importance score.

    Components:
    - w0 * log(1 + access_count)  — frequently accessed = important
    - w1 * min(tag_count / 5, 1)  — well-tagged = curated
    - w2 * log(1 + relations)     — connected = contextual
    - w3 * recency_factor         — recent = relevant

    Returns: score in [0, 1] range.
    """
    w0, w1, w2, w3 = weights[:4] if len(weights) >= 4 else (0.3, 0.2, 0.3, 0.2)

    access_score = math.log1p(access_count) / math.log1p(100)  # normalized to ~1.0 at 100
    tag_score = min(tag_count / 5.0, 1.0)
    relation_score = math.log1p(relation_count) / math.log1p(20)
    recency_score = math.exp(-0.01 * max(age_days, 0))  # slow decay

    raw = w0 * access_score + w1 * tag_score + w2 * relation_score + w3 * recency_score
    return min(max(raw, 0.0), 1.0)


def apply_importance_boost(
    results: list[SearchResult],
    importance_scores: dict[str, float],
    max_boost: float = 1.5,
) -> list[SearchResult]:
    """Apply importance-based boost to search results and re-sort."""
    from memtomem.models import SearchResult as SR

    if not results:
        return results

    boosted = []
    for r in results:
        imp = importance_scores.get(str(r.chunk.id), 0.0)
        factor = 1.0 + (max_boost - 1.0) * imp  # imp in [0,1] maps to boost [1.0, max_boost]
        boosted.append(SR(chunk=r.chunk, score=r.score * factor, rank=r.rank, source=r.source))

    boosted.sort(key=lambda r: r.score, reverse=True)
    return [
        SR(chunk=r.chunk, score=r.score, rank=i + 1, source=r.source) for i, r in enumerate(boosted)
    ]
