"""MMR (Maximal Marginal Relevance) diversity re-ranking."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from memtomem.models import SearchResult


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors (pure Python, no numpy)."""
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a < 1e-10 or norm_b < 1e-10:
        return 0.0
    return dot / (norm_a * norm_b)


def apply_mmr(
    results: list[SearchResult],
    embeddings: dict[UUID, list[float]],
    lambda_param: float = 0.7,
    top_k: int | None = None,
) -> list[SearchResult]:
    """Re-rank results using Maximal Marginal Relevance.

    MMR = argmax_{d in R\\S} [ lambda * Sim(d, q) - (1-lambda) * max_{d' in S} Sim(d, d') ]

    The first document is always the highest-scoring one. Subsequent documents
    are chosen greedily to maximize MMR score.

    Args:
        results: Candidate search results (pre-sorted by relevance score).
        embeddings: Mapping from chunk ID to embedding vector.
        lambda_param: Relevance vs diversity tradeoff (1.0 = pure relevance,
            0.0 = pure diversity).
        top_k: Maximum number of results to return. Defaults to len(results).

    Returns:
        Re-ranked list of SearchResult with updated ranks and source="mmr".
    """
    from memtomem.models import SearchResult as SR

    if not results:
        return []

    if top_k is None:
        top_k = len(results)

    # Short-circuit: single result needs no MMR
    if len(results) <= 1:
        return [
            SR(chunk=r.chunk, score=r.score, rank=i + 1, source="mmr")
            for i, r in enumerate(results)
        ]

    # Normalize scores to [0, 1] for MMR computation
    max_score = max(r.score for r in results) if results else 1.0
    if max_score < 1e-10:
        max_score = 1.0

    # Pre-compute pairwise similarity matrix -- O(n^2) once, then O(1) lookups
    ids = [r.chunk.id for r in results]
    embs = {cid: embeddings.get(cid, []) for cid in ids}
    sim_cache: dict[tuple[UUID, UUID], float] = {}
    for i_idx, id_a in enumerate(ids):
        ea = embs[id_a]
        for j_idx in range(i_idx + 1, len(ids)):
            id_b = ids[j_idx]
            eb = embs[id_b]
            s = cosine_similarity(ea, eb) if ea and eb else 0.0
            sim_cache[(id_a, id_b)] = s
            sim_cache[(id_b, id_a)] = s

    selected: list[SearchResult] = []
    remaining = list(results)

    # First pick: highest score
    first = remaining.pop(0)
    selected.append(first)

    while remaining and len(selected) < top_k:
        best_mmr = float("-inf")
        best_idx = 0

        for i, candidate in enumerate(remaining):
            cid = candidate.chunk.id

            # Relevance term (normalized)
            relevance = candidate.score / max_score

            # Diversity penalty: max similarity to any already selected doc
            max_sim = max(
                (sim_cache.get((cid, sel.chunk.id), 0.0) for sel in selected),
                default=0.0,
            )

            mmr_score = lambda_param * relevance - (1 - lambda_param) * max_sim

            if mmr_score > best_mmr:
                best_mmr = mmr_score
                best_idx = i

        selected.append(remaining.pop(best_idx))

    # Rebuild with updated ranks and source
    return [
        SR(chunk=r.chunk, score=r.score, rank=i + 1, source="mmr") for i, r in enumerate(selected)
    ]
