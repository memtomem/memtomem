"""Local ONNX embedding provider using fastembed."""

from __future__ import annotations

import asyncio
import logging
from typing import Sequence

from memtomem.config import EmbeddingConfig
from memtomem.errors import EmbeddingError

logger = logging.getLogger(__name__)

# Short name -> (fastembed model ID, dimension).
# Users may also pass a raw fastembed model ID via config.model.
_ONNX_MODELS: dict[str, tuple[str, int]] = {
    "all-MiniLM-L6-v2": ("sentence-transformers/all-MiniLM-L6-v2", 384),
    "bge-small-en-v1.5": ("BAAI/bge-small-en-v1.5", 384),
    "bge-m3": ("BAAI/bge-m3", 1024),
}


def _resolve_model(name: str) -> str:
    """Map a short model name to the fastembed model ID."""
    entry = _ONNX_MODELS.get(name)
    if entry:
        return entry[0]
    return name  # pass through as raw fastembed model ID


def _register_custom_models_if_needed() -> None:
    """Register models that fastembed >=0.4 dropped from its built-in catalog.

    fastembed 0.8.0's ``TextEmbedding`` no longer ships ``BAAI/bge-m3`` (the
    model type split across dedicated classes none of which currently host
    it). Re-register it from the official HF ONNX export so existing installs
    keep working without changing the user-facing model name.
    """
    from fastembed import TextEmbedding  # type: ignore[import-untyped]
    from fastembed.common.model_description import (  # type: ignore[import-untyped]
        ModelSource,
        PoolingType,
    )

    registered = {m.get("model") for m in TextEmbedding.list_supported_models()}
    if "BAAI/bge-m3" not in registered:
        TextEmbedding.add_custom_model(
            model="BAAI/bge-m3",
            pooling=PoolingType.CLS,
            normalization=True,
            sources=ModelSource(hf="BAAI/bge-m3"),
            dim=1024,
            model_file="onnx/model.onnx",
            additional_files=["onnx/model.onnx_data"],
            size_in_gb=2.3,
        )


class OnnxEmbedder:
    """Embedding provider backed by fastembed (ONNX Runtime).

    Runs entirely on the CPU — no external server or GPU required.
    Install the optional dependency: ``pip install memtomem[onnx]``
    """

    def __init__(self, config: EmbeddingConfig) -> None:
        self._config = config
        self._model: object | None = None  # fastembed.TextEmbedding

    def _get_model(self) -> object:
        """Lazily initialise the fastembed model (downloads on first use)."""
        if self._model is not None:
            return self._model
        try:
            from fastembed import TextEmbedding  # type: ignore[import-untyped]
        except ImportError as exc:
            raise EmbeddingError(
                "fastembed is required for the ONNX embedding provider. "
                "Install it with: pip install memtomem[onnx]"
            ) from exc

        _register_custom_models_if_needed()
        model_id = _resolve_model(self._config.model)
        logger.info("Loading ONNX embedding model %s …", model_id)
        self._model = TextEmbedding(model_name=model_id)
        return self._model

    @property
    def dimension(self) -> int:
        return self._config.dimension

    @property
    def model_name(self) -> str:
        return self._config.model

    def _embed_sync(self, texts: list[str]) -> list[list[float]]:
        """Run inference synchronously — called inside ``to_thread``."""
        model = self._get_model()
        # model.embed() returns a generator of numpy arrays;
        # materialize fully inside the thread to avoid blocking the
        # event loop with lazy evaluation.
        return [vec.tolist() for vec in model.embed(texts)]

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            return await asyncio.to_thread(self._embed_sync, list(texts))
        except EmbeddingError:
            raise
        except Exception as exc:
            raise EmbeddingError(f"ONNX embedding failed: {exc}") from exc

    async def embed_query(self, query: str) -> list[float]:
        if not query or not query.strip():
            raise EmbeddingError("Query text cannot be empty")
        embeddings = await self.embed_texts([query])
        if not embeddings:
            raise EmbeddingError("No embeddings returned for query")
        return embeddings[0]

    async def close(self) -> None:
        self._model = None
