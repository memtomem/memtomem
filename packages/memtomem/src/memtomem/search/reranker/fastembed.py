"""FastEmbed cross-encoder reranker — local ONNX, no external service."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from memtomem.config import RerankConfig
    from memtomem.models import SearchResult

logger = logging.getLogger(__name__)


class FastEmbedReranker:
    """Cross-encoder reranking via ``fastembed.rerank.cross_encoder.TextCrossEncoder``.

    Runs on the CPU via ONNX Runtime — no external server and no PyTorch
    dependency. Reuses the ``memtomem[onnx]`` extra so enabling this provider
    adds no new packages. The model is downloaded on first use and cached in
    ``~/.cache/fastembed/``.
    """

    def __init__(self, config: RerankConfig) -> None:
        self._config = config
        self._model: object | None = None

    def _get_model(self) -> object:
        """Lazily construct the ``TextCrossEncoder`` — downloads on first use."""
        if self._model is not None:
            return self._model
        try:
            from fastembed.rerank.cross_encoder import (  # type: ignore[import-untyped]
                TextCrossEncoder,
            )
        except ImportError as exc:
            raise ImportError(
                "fastembed is required for the fastembed reranker. "
                "Install it with: pip install memtomem[onnx]"
            ) from exc

        try:
            self._model = TextCrossEncoder(model_name=self._config.model)
        except ValueError as exc:
            supported = [m.get("model", "") for m in TextCrossEncoder.list_supported_models()]
            raise ValueError(
                f"fastembed reranker model {self._config.model!r} is not supported. "
                f"Built-in options: {', '.join(sorted(s for s in supported if s))}. "
                "For Korean/Chinese/Japanese try "
                "'jinaai/jina-reranker-v2-base-multilingual' (1.1 GB); for lightweight "
                "English 'Xenova/ms-marco-MiniLM-L-6-v2' (80 MB). Custom ONNX exports "
                "must be registered via TextCrossEncoder.add_custom_model() before the "
                "reranker is invoked."
            ) from exc
        logger.info("Loaded fastembed reranker: %s", self._config.model)
        return self._model

    def _rerank_sync(self, query: str, documents: list[str]) -> list[float]:
        """Run inference synchronously — called inside ``asyncio.to_thread``."""
        model = self._get_model()
        # ``rerank`` returns an iterable of floats; materialize inside the
        # thread so the caller doesn't block on lazy evaluation.
        return [float(s) for s in model.rerank(query, documents)]  # type: ignore[attr-defined]

    async def rerank(
        self, query: str, results: list[SearchResult], top_k: int
    ) -> list[SearchResult]:
        from memtomem.models import SearchResult as SR

        if not results:
            return results

        documents = [r.chunk.content for r in results]
        try:
            scores = await asyncio.to_thread(self._rerank_sync, query, documents)
        except (ImportError, ValueError):
            # Setup/config errors carry actionable hints — surface, don't hide.
            raise
        except Exception as exc:
            logger.warning("FastEmbed rerank failed, returning original order: %s", exc)
            return results[:top_k]

        scored = sorted(zip(scores, results), key=lambda x: x[0], reverse=True)
        return [
            SR(chunk=r.chunk, score=float(s), rank=i + 1, source="reranked")
            for i, (s, r) in enumerate(scored[:top_k])
        ]

    async def close(self) -> None:
        self._model = None
