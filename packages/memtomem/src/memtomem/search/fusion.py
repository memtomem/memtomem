"""Reciprocal Rank Fusion (RRF) for merging ranked result lists."""

from __future__ import annotations

import heapq
from uuid import UUID

from memtomem.models import Chunk, SearchResult


def reciprocal_rank_fusion(
    result_lists: list[list[SearchResult]],
    k: int = 60,
    top_k: int = 10,
    weights: list[float] | None = None,
) -> list[SearchResult]:
    """Merge multiple ranked lists using weighted RRF.

    score(d) = sum over all lists: weight[i] / (k + rank(d))

    When weights is None or all equal, behaves like standard RRF.
    """
    if weights is None:
        weights = [1.0] * len(result_lists)
    elif len(weights) < len(result_lists):
        weights = list(weights) + [1.0] * (len(result_lists) - len(weights))

    scores: dict[UUID, float] = {}
    chunk_map: dict[UUID, Chunk] = {}
    hit_counts: dict[UUID, int] = {}

    for list_idx, result_list in enumerate(result_lists):
        w = weights[list_idx]
        for rank_0, result in enumerate(result_list):
            rank = rank_0 + 1  # 1-based
            cid = result.chunk.id
            scores[cid] = scores.get(cid, 0.0) + w / (k + rank)
            chunk_map[cid] = result.chunk
            hit_counts[cid] = hit_counts.get(cid, 0) + 1

    top_items = heapq.nlargest(top_k, scores.items(), key=lambda x: x[1])

    # Label by retrieval source: "fused" = appeared in multiple lists
    list_labels = ["bm25", "dense"]

    def _source_label(cid: UUID) -> str:
        hits = hit_counts.get(cid, 0)
        if hits >= len(result_lists):
            return "fused"
        for label, result_list in zip(list_labels, result_lists):
            if any(r.chunk.id == cid for r in result_list):
                return label
        return "fused"

    return [
        SearchResult(
            chunk=chunk_map[cid],
            score=score,
            rank=i + 1,
            source=_source_label(cid),
        )
        for i, (cid, score) in enumerate(top_items)
    ]
