"""Pure-function IR metrics for retrieval regression tests.

Functions operate on a single query's ranking. Callers aggregate across queries
(e.g., ``statistics.fmean(recall_at_k(...) for q in queries)`` for mean recall).

The ``retrieved`` argument is an ordered list of IDs from rank 1 downward.
``relevant`` is a set of IDs considered relevant for the query. ``relevance``
(for NDCG) is a dict mapping ID to a non-negative gain — missing IDs count as 0.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping


def recall_at_k(retrieved: list[str], relevant: set[str] | frozenset[str], k: int) -> float:
    """Fraction of relevant items found in the top-k retrieved.

    Returns 0.0 when there are no relevant items (undefined recall → treated as miss
    so a caller averaging across queries isn't biased by empty-relevance entries).
    """
    if k <= 0 or not relevant:
        return 0.0
    hits = sum(1 for item in retrieved[:k] if item in relevant)
    return hits / len(relevant)


def reciprocal_rank_at_k(
    retrieved: list[str], relevant: set[str] | frozenset[str], k: int
) -> float:
    """Reciprocal rank of the first relevant hit within top-k, or 0.0 if none.

    Mean reciprocal rank (MRR) across queries is ``statistics.fmean`` of this.
    """
    if k <= 0:
        return 0.0
    for rank, item in enumerate(retrieved[:k], start=1):
        if item in relevant:
            return 1.0 / rank
    return 0.0


def ndcg_at_k(retrieved: list[str], relevance: Mapping[str, float], k: int) -> float:
    """Normalized DCG@k with the standard ``rel / log2(rank + 1)`` gain.

    Missing IDs in ``relevance`` count as zero gain. Returns 0.0 when the ideal
    DCG is zero (no positive-relevance items known at all).
    """
    if k <= 0 or not relevance:
        return 0.0
    dcg = 0.0
    for rank, item in enumerate(retrieved[:k], start=1):
        gain = relevance.get(item, 0.0)
        if gain > 0:
            dcg += gain / math.log2(rank + 1)
    ideal_gains = sorted((g for g in relevance.values() if g > 0), reverse=True)[:k]
    idcg = sum(g / math.log2(rank + 1) for rank, g in enumerate(ideal_gains, start=1))
    return dcg / idcg if idcg > 0 else 0.0


def mean(values: Iterable[float]) -> float:
    """Arithmetic mean, returning 0.0 on empty input (stdlib ``mean`` raises)."""
    xs = list(values)
    return sum(xs) / len(xs) if xs else 0.0
