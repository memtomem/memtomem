"""Local cross-encoder reranker using sentence-transformers."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from memtomem.config import RerankConfig
    from memtomem.models import SearchResult

logger = logging.getLogger(__name__)


class LocalReranker:
    """Cross-encoder reranking using a local sentence-transformers model."""

    def __init__(self, config: RerankConfig):
        self._config = config
        self._model = None

    def _get_model(self):
        if self._model is None:
            from sentence_transformers import CrossEncoder

            self._model = CrossEncoder(self._config.model)
            logger.info("Loaded local reranker: %s", self._config.model)
        return self._model

    async def rerank(
        self, query: str, results: list[SearchResult], top_k: int
    ) -> list[SearchResult]:
        from memtomem.models import SearchResult as SR

        if not results:
            return results

        model = self._get_model()
        pairs = [(query, r.chunk.content) for r in results]

        try:
            scores = model.predict(pairs)
        except Exception as exc:
            logger.warning("Local rerank failed, returning original order: %s", exc)
            return results[:top_k]

        scored = sorted(zip(scores, results), key=lambda x: x[0], reverse=True)

        return [
            SR(chunk=r.chunk, score=float(s), rank=i + 1, source="reranked")
            for i, (s, r) in enumerate(scored[:top_k])
        ]

    async def close(self) -> None:
        self._model = None
