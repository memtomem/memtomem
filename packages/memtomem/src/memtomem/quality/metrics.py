"""Pure-function IR metrics for retrieval evaluation.

Functions operate on a single query's ranking. Callers aggregate across queries
(e.g., ``mean(recall_at_k(...) for q in queries)`` for mean recall).

The ``retrieved`` argument is an ordered list of IDs from rank 1 downward.
``relevant`` is a set of IDs considered relevant for the query. ``relevance``
(for NDCG) is a dict mapping ID to a non-negative gain — missing IDs count as 0.

Identity note (#1802): the Quality Lab keys results by ``content_hash``, and
several chunks can share one hash, so a ranking may contain duplicate IDs. The
replay layer deduplicates by identity before calling these functions; as defense
in depth, ``recall_at_k`` / ``recall_labeled_at_k`` / ``precision_at_k`` count
*distinct* hits so a duplicated relevant item cannot push a rate above 1.0. For
rankings of unique IDs (the existing retrieval-regression callers) this is
identical to the previous occurrence-counting behavior.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping

__all__ = [
    "recall_at_k",
    "recall_labeled_at_k",
    "reciprocal_rank_at_k",
    "ndcg_at_k",
    "precision_at_k",
    "hit_rate_at_k",
    "mean",
]


def recall_at_k(retrieved: list[str], relevant: set[str] | frozenset[str], k: int) -> float:
    """Fraction of relevant items found in the top-k retrieved.

    Returns 0.0 when there are no relevant items (undefined recall → treated as miss
    so a caller averaging across queries isn't biased by empty-relevance entries).
    Counts distinct relevant hits, so a duplicated relevant ID cannot exceed 1.0.
    """
    if k <= 0 or not relevant:
        return 0.0
    hits = len(set(retrieved[:k]) & set(relevant))
    return hits / len(relevant)


def recall_labeled_at_k(retrieved: list[str], relevant: set[str] | frozenset[str], k: int) -> float:
    """Recall over the *labeled*-relevant set (denominator = labeled relevant).

    Named explicitly to signal this is not true corpus recall: the denominator is
    only the items a judge marked relevant, not every relevant item in the corpus.
    Identical computation to :func:`recall_at_k`; the distinct name keeps report
    fields honest about what the number means.
    """
    return recall_at_k(retrieved, relevant, k)


def reciprocal_rank_at_k(
    retrieved: list[str], relevant: set[str] | frozenset[str], k: int
) -> float:
    """Reciprocal rank of the first relevant hit within top-k, or 0.0 if none.

    Mean reciprocal rank (MRR) across queries is the ``mean`` of this.
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


def precision_at_k(
    retrieved: list[str],
    relevant: set[str] | frozenset[str],
    not_relevant: set[str] | frozenset[str],
    k: int,
) -> float | None:
    """Precision@k, or ``None`` when the top-k labels are incomplete.

    Precision needs every retrieved item in the window to carry a judgment:
    a partially labeled window would silently treat unlabeled items as
    non-relevant and bias the number downward. When any of the considered items
    is neither in ``relevant`` nor ``not_relevant`` — or when no items are
    retrieved — the result is ``None`` (report it as ``incomplete_labels``),
    which an aggregate must exclude rather than average in.

    Counts distinct relevant hits over the distinct considered window.
    """
    if k <= 0:
        return None
    window = retrieved[:k]
    if not window:
        return None
    labeled = set(relevant) | set(not_relevant)
    considered = set(window)
    if not considered <= labeled:
        return None
    hits = len(considered & set(relevant))
    return hits / len(considered)


def hit_rate_at_k(retrieved: list[str], relevant: set[str] | frozenset[str], k: int) -> float:
    """1.0 if any relevant item appears in the top-k, else 0.0."""
    if k <= 0 or not relevant:
        return 0.0
    return 1.0 if set(retrieved[:k]) & set(relevant) else 0.0


def mean(values: Iterable[float]) -> float:
    """Arithmetic mean, returning 0.0 on empty input (stdlib ``mean`` raises)."""
    xs = list(values)
    return sum(xs) / len(xs) if xs else 0.0
