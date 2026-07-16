"""Local cross-encoder reranker using sentence-transformers."""

from __future__ import annotations

import logging
import threading
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
        self._closed = False
        # Serializes the first load — same contract as ``OnnxEmbedder``:
        # the search path and the opt-in warmup task (#1621) can race into
        # ``_get_model`` from different threads.
        self._load_lock = threading.Lock()

    def _get_model(self):
        # A closed instance must not resurrect: reloading the released model
        # here is silent expensive work on an instance nobody owns (#1778).
        # Cached reads go through a local snapshot so a concurrent close()
        # nulling ``_model`` between the check and the return cannot hand
        # the caller ``None``.
        if self._closed:
            raise RuntimeError("LocalReranker is closed")
        model = self._model
        if model is None:
            with self._load_lock:
                # Re-check under the lock: a warmup/readiness thread that
                # passed the guard above can lose the race to a concurrent
                # close() — loading here would resurrect the model onto the
                # closed instance (#1778).
                if self._closed:
                    raise RuntimeError("LocalReranker is closed")
                model = self._model
                if model is None:
                    from sentence_transformers import CrossEncoder

                    model = CrossEncoder(self._config.model)
                    # Publish-then-verify: close() does not take this lock,
                    # so it can land while the construction above is in
                    # flight. Publishing first and re-checking makes every
                    # interleaving of close()'s (flag, _model=None) writes
                    # end with the closed instance holding no model (#1778).
                    self._model = model
                    if self._closed:
                        self._model = None
                        raise RuntimeError("LocalReranker is closed")
                    logger.info("Loaded local reranker: %s", self._config.model)
        return model

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
        self._closed = True
        self._model = None
