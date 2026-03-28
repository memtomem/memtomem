"""Reranker protocol."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from memtomem.models import SearchResult


class Reranker(Protocol):
    """Protocol for cross-encoder reranking providers."""

    async def rerank(
        self, query: str, results: list[SearchResult], top_k: int
    ) -> list[SearchResult]:
        """Rerank search results using a cross-encoder model.

        Args:
            query: The search query.
            results: Candidate results from RRF fusion.
            top_k: Maximum results to return.

        Returns:
            Re-scored and re-sorted results with source="reranked".
        """
        ...

    async def close(self) -> None:
        """Release resources."""
        ...
