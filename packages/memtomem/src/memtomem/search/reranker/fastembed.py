"""FastEmbed cross-encoder reranker — local ONNX, no external service."""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import TYPE_CHECKING

from memtomem.embedding.fastembed_cache import resolve_fastembed_cache_dir

if TYPE_CHECKING:
    from memtomem.config import RerankConfig
    from memtomem.models import SearchResult

logger = logging.getLogger(__name__)


class FastEmbedReranker:
    """Cross-encoder reranking via ``fastembed.rerank.cross_encoder.TextCrossEncoder``.

    Runs on the CPU via ONNX Runtime — no external server and no PyTorch
    dependency. Reuses the ``memtomem[onnx]`` extra so enabling this provider
    adds no new packages. The model is downloaded on first use and cached in
    the path returned by ``resolve_fastembed_cache_dir()`` (default
    ``~/.memtomem/cache/fastembed``).
    """

    def __init__(self, config: RerankConfig) -> None:
        self._config = config
        self._model: object | None = None
        self._closed = False
        # Observability flags read by ``GET /api/system/model-readiness``.
        # Match ``OnnxEmbedder`` so the endpoint can introspect both via a
        # single contract without each provider having a bespoke surface.
        self._loading: bool = False
        self._load_error: str | None = None
        # Serializes the first load — same contract as ``OnnxEmbedder``:
        # the search path and the opt-in warmup task (#1621) can race into
        # ``_get_model`` from different threads.
        self._load_lock = threading.Lock()

    def _get_model(self) -> object:
        """Lazily construct the ``TextCrossEncoder`` — downloads on first use.

        Double-checked lock so concurrent first-callers (search path vs
        warmup) share a single construction.
        """
        # A closed instance must not resurrect: re-downloading/re-initializing
        # the released ONNX model here is silent expensive work on an
        # instance nobody owns (#1778).
        if self._closed:
            raise RuntimeError("FastEmbedReranker is closed")
        if self._model is not None:
            return self._model
        with self._load_lock:
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

            cache_dir = resolve_fastembed_cache_dir()
            logger.info(
                "Loading fastembed reranker %s (cache_dir=%s) …",
                self._config.model,
                cache_dir,
            )
            self._loading = True
            self._load_error = None
            try:
                self._model = TextCrossEncoder(
                    model_name=self._config.model, cache_dir=str(cache_dir)
                )
            except ValueError as exc:
                supported = [m.get("model", "") for m in TextCrossEncoder.list_supported_models()]
                self._load_error = str(exc)
                raise ValueError(
                    f"fastembed reranker model {self._config.model!r} is not supported. "
                    f"Built-in options: {', '.join(sorted(s for s in supported if s))}. "
                    "For Korean/Chinese/Japanese try "
                    "'jinaai/jina-reranker-v2-base-multilingual' (1.1 GB); for lightweight "
                    "English 'Xenova/ms-marco-MiniLM-L-6-v2' (80 MB). Custom ONNX exports "
                    "must be registered via TextCrossEncoder.add_custom_model() before the "
                    "reranker is invoked."
                ) from exc
            except Exception as exc:
                self._load_error = str(exc)
                raise
            finally:
                self._loading = False
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
        # Same shape as ``OnnxEmbedder.close``: force-collect so the underlying
        # ORT InferenceSession releases its mmap and thread-local arenas before
        # pytest cleans up tmp_path on Windows. See #206.
        import gc

        self._closed = True
        self._model = None
        gc.collect()
