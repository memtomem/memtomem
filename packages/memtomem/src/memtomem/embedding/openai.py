"""OpenAI-compatible embedding provider (works with any /v1/embeddings endpoint)."""

from __future__ import annotations

import logging

import httpx

from memtomem.config import EmbeddingConfig
from memtomem.embedding.retry import _parse_retry_after, with_retry
from memtomem.errors import EmbeddingError

logger = logging.getLogger(__name__)


class _RateLimitError(Exception):
    """Raised on HTTP 429 to trigger retry via with_retry decorator."""

    def __init__(self, retry_after: float | None = None) -> None:
        self.retry_after = retry_after
        super().__init__(f"Rate limited (retry_after={retry_after})")


class OpenAIEmbedder:
    """Calls any OpenAI-compatible /v1/embeddings endpoint.

    Set base_url to a custom host (e.g. Azure OpenAI, local vLLM) via config.
    """

    def __init__(self, config: EmbeddingConfig) -> None:
        self._config = config
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            base = (self._config.base_url or "https://api.openai.com").rstrip("/")
            headers: dict[str, str] = {}
            if self._config.api_key:
                headers["Authorization"] = f"Bearer {self._config.api_key}"
            self._client = httpx.AsyncClient(
                base_url=base,
                headers=headers,
                timeout=60.0,
            )
        return self._client

    @with_retry(
        max_attempts=4,
        base_delay=1.0,
        retryable_exceptions=(httpx.ConnectError, httpx.TimeoutException, _RateLimitError),
    )
    async def _embed_batch_with_retry(self, batch: list[str]) -> list[list[float]]:
        """Send a single batch to OpenAI with retry on transient errors and 429."""
        client = self._get_client()
        resp = await client.post(
            "/v1/embeddings",
            json={"input": batch, "model": self._config.model},
        )
        if resp.status_code == 429:
            ra_val = _parse_retry_after(resp.headers.get("retry-after"))
            raise _RateLimitError(retry_after=ra_val)
        resp.raise_for_status()
        data = resp.json()["data"]
        data.sort(key=lambda x: x["index"])
        return [item["embedding"] for item in data]

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        import asyncio

        if not texts:
            return []

        bs = self._config.batch_size
        batches = [texts[i : i + bs] for i in range(0, len(texts), bs)]
        sem = asyncio.Semaphore(self._config.max_concurrent_batches)

        async def _safe_embed(batch: list[str]) -> list[list[float]]:
            async with sem:
                return await self._embed_batch_with_retry(batch)

        try:
            batch_results = await asyncio.gather(*[_safe_embed(b) for b in batches])
        except httpx.ConnectError as exc:
            raise EmbeddingError(
                f"Cannot connect to OpenAI API. "
                f"Check your network connection and base_url. Error: {exc}"
            ) from exc
        except httpx.TimeoutException as exc:
            raise EmbeddingError(
                f"OpenAI embedding request timed out. The API may be overloaded. Error: {exc}"
            ) from exc
        except _RateLimitError as exc:
            raise EmbeddingError(
                "OpenAI API rate limit exceeded after retries. "
                "Please wait before retrying or upgrade your plan."
            ) from exc
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 401:
                raise EmbeddingError(
                    "OpenAI API authentication failed. "
                    "Verify your API key is valid and set correctly."
                ) from exc
            raise EmbeddingError(f"OpenAI embedding request failed: {exc}") from exc
        except httpx.HTTPError as exc:
            raise EmbeddingError(f"OpenAI embedding request failed: {exc}") from exc

        results: list[list[float]] = []
        for br in batch_results:
            results.extend(br)
        return results

    async def embed_query(self, text: str) -> list[float]:
        if not text or not text.strip():
            raise EmbeddingError("Query text cannot be empty")
        embeddings = await self.embed_texts([text])
        if not embeddings:
            raise EmbeddingError("No embeddings returned for query")
        return embeddings[0]

    async def close(self) -> None:
        if self._client:
            try:
                await self._client.aclose()
            except Exception:
                logger.debug("Failed to close HTTP client", exc_info=True)
            self._client = None
