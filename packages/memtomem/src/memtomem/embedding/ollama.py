"""Ollama embedding provider using httpx."""

from __future__ import annotations

import logging
from typing import Sequence

import httpx

from memtomem.config import EmbeddingConfig
from memtomem.embedding.retry import with_retry
from memtomem.errors import EmbeddingError

logger = logging.getLogger(__name__)


class OllamaEmbedder:
    def __init__(self, config: EmbeddingConfig) -> None:
        self._config = config
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._config.base_url or "http://localhost:11434",
                timeout=60.0,
            )
        return self._client

    @property
    def dimension(self) -> int:
        return self._config.dimension

    @property
    def model_name(self) -> str:
        return self._config.model

    @with_retry(
        max_attempts=3,
        base_delay=1.0,
        retryable_exceptions=(httpx.ConnectError, httpx.TimeoutException),
    )
    async def _embed_batch_with_retry(self, batch: list[str]) -> list[list[float]]:
        """Send a single batch to Ollama with retry on transient errors."""
        client = self._get_client()
        resp = await client.post(
            "/api/embed",
            json={"model": self._config.model, "input": batch},
        )
        resp.raise_for_status()
        data = resp.json()
        embeddings = data.get("embeddings")
        if embeddings is None:
            raise EmbeddingError(
                f"Ollama API returned unexpected response (missing 'embeddings' key): {list(data.keys())}"
            )
        return embeddings

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        import asyncio

        bs = self._config.batch_size
        batches = [list(texts[i : i + bs]) for i in range(0, len(texts), bs)]
        sem = asyncio.Semaphore(self._config.max_concurrent_batches)

        async def _safe_embed(batch: list[str]) -> list[list[float]]:
            async with sem:
                return await self._embed_batch_with_retry(batch)

        try:
            batch_results = await asyncio.gather(*[_safe_embed(b) for b in batches])
        except httpx.ConnectError as e:
            raise EmbeddingError(
                f"Cannot connect to Ollama at {self._config.base_url}. "
                f"Please verify 'ollama serve' is running. Error: {e}"
            ) from e
        except httpx.TimeoutException as e:
            raise EmbeddingError(
                f"Ollama embedding request timed out. "
                f"The model '{self._config.model}' may still be loading. Error: {e}"
            ) from e
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                raise EmbeddingError(
                    f"Model '{self._config.model}' not found in Ollama. "
                    f"Run 'ollama pull {self._config.model}' to download it."
                ) from e
            raise EmbeddingError(f"Ollama embedding failed: {e}") from e
        except httpx.HTTPError as e:
            raise EmbeddingError(f"Ollama embedding failed: {e}") from e

        results: list[list[float]] = []
        for br in batch_results:
            results.extend(br)
        return results

    async def embed_query(self, query: str) -> list[float]:
        if not query or not query.strip():
            raise EmbeddingError("Query text cannot be empty")
        embeddings = await self.embed_texts([query])
        if not embeddings:
            raise EmbeddingError("No embeddings returned for query")
        return embeddings[0]

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                logger.debug("Failed to close HTTP client", exc_info=True)
            self._client = None
