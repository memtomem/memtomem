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
    list_labels: list[str] | None = None,
) -> list[SearchResult]:
    """Merge multiple ranked lists using weighted RRF.

    score(d) = sum over all lists: weight[i] / (k + rank(d))

    When weights is None or all equal, behaves like standard RRF.
    ``list_labels`` lets the caller name each input list (defaults to
    ``["bm25", "dense", ...]``). A fused result's ``source`` reports
    the originating list when the chunk hit only one input, or
    ``"fused"`` when it hit more than one.

    ``via_session_summary`` propagates with OR semantics: if any input
    leg carried the flag for a chunk, the merged result keeps it.
    """
    if weights is None:
        weights = [1.0] * len(result_lists)
    elif len(weights) < len(result_lists):
        weights = list(weights) + [1.0] * (len(result_lists) - len(weights))

    if list_labels is None:
        list_labels = ["bm25", "dense"]
    if len(list_labels) < len(result_lists):
        list_labels = list(list_labels) + [
            f"leg{i}" for i in range(len(list_labels), len(result_lists))
        ]

    scores: dict[UUID, float] = {}
    chunk_map: dict[UUID, Chunk] = {}
    hit_counts: dict[UUID, int] = {}
    hit_lists: dict[UUID, list[int]] = {}
    via_summary: dict[UUID, bool] = {}

    for list_idx, result_list in enumerate(result_lists):
        w = weights[list_idx]
        for rank_0, result in enumerate(result_list):
            rank = rank_0 + 1  # 1-based
            cid = result.chunk.id
            scores[cid] = scores.get(cid, 0.0) + w / (k + rank)
            chunk_map[cid] = result.chunk
            hit_counts[cid] = hit_counts.get(cid, 0) + 1
            hit_lists.setdefault(cid, []).append(list_idx)
            if result.via_session_summary:
                via_summary[cid] = True

    top_items = heapq.nlargest(top_k, scores.items(), key=lambda x: x[1])

    def _source_label(cid: UUID) -> str:
        if hit_counts.get(cid, 0) >= 2:
            return "fused"
        idxs = hit_lists.get(cid, [])
        if not idxs:
            return "fused"
        return list_labels[idxs[0]]

    return [
        SearchResult(
            chunk=chunk_map[cid],
            score=score,
            rank=i + 1,
            source=_source_label(cid),
            via_session_summary=via_summary.get(cid, False),
        )
        for i, (cid, score) in enumerate(top_items)
    ]
