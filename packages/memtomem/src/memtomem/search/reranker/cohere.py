"""Cohere rerank provider."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from memtomem.config import RerankConfig
    from memtomem.models import SearchResult

logger = logging.getLogger(__name__)


class CohereReranker:
    """Cross-encoder reranking via Cohere Rerank API."""

    def __init__(self, config: RerankConfig):
        self._config = config
        self._client = None

    def _get_client(self):
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def rerank(
        self, query: str, results: list[SearchResult], top_k: int
    ) -> list[SearchResult]:
        from memtomem.models import SearchResult as SR

        if not results:
            return results

        documents = [r.chunk.content for r in results]
        client = self._get_client()

        try:
            resp = await client.post(
                "https://api.cohere.ai/v1/rerank",
                json={
                    "model": self._config.model,
                    "query": query,
                    "documents": documents,
                    "top_n": min(top_k, len(documents)),
                },
                headers={
                    "Authorization": f"Bearer {self._config.api_key}",
                    "Content-Type": "application/json",
                },
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.warning("Cohere rerank failed, returning original order: %s", exc)
            return results[:top_k]

        reranked = []
        for i, item in enumerate(data.get("results", [])):
            idx = item["index"]
            orig = results[idx]
            reranked.append(
                SR(
                    chunk=orig.chunk,
                    score=item["relevance_score"],
                    rank=i + 1,
                    source="reranked",
                )
            )

        return reranked[:top_k]

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None
