"""Reranker protocol."""

from __future__ import annotations

import inspect
import logging
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from memtomem.models import SearchResult

logger = logging.getLogger(__name__)


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


async def close_reranker_safely(reranker: object) -> None:
    """Close a reranker, tolerating sync/async/missing close + errors.

    Shared by the web hot-reload/PATCH swap paths and the pipeline's
    deferred-close path (#1777) so a flaky teardown never propagates —
    a failed close is logged, not raised.
    """
    close = getattr(reranker, "close", None)
    if not callable(close):
        return

    try:
        result = close()
        if inspect.isawaitable(result):
            await result
    except Exception:
        logger.exception("Error while closing replaced reranker")
