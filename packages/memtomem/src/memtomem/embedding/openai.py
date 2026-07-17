"""OpenAI-compatible embedding provider (works with any /v1/embeddings endpoint)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Sequence

import httpx

from memtomem.config import EmbeddingConfig
from memtomem.embedding.retry import RateLimitError, parse_retry_after, with_retry
from memtomem.errors import EmbeddingError

logger = logging.getLogger(__name__)


class OpenAIEmbedder:
    """Calls any OpenAI-compatible /v1/embeddings endpoint.

    Set base_url to a custom host (e.g. Azure OpenAI, local vLLM) via config.
    """

    # Remote HTTP embedding is latency-bound, so concurrent per-file
    # ``embed_texts`` calls genuinely overlap; match the index engine's
    # file-level cap to preserve pre-#1783 throughput. Intra-file batch
    # fan-out is still self-limited by ``max_concurrent_batches`` below.
    preferred_concurrency = 8

    def __init__(self, config: EmbeddingConfig) -> None:
        self._config = config
        self._client: httpx.AsyncClient | None = None

    @property
    def dimension(self) -> int:
        return self._config.dimension

    @property
    def model_name(self) -> str:
        return self._config.model

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
        retryable_exceptions=(httpx.ConnectError, httpx.TimeoutException, RateLimitError),
    )
    async def _embed_batch_with_retry(self, batch: list[str]) -> list[list[float]]:
        """Send a single batch to OpenAI with retry on transient errors and 429."""
        client = self._get_client()
        resp = await client.post(
            "/v1/embeddings",
            json={"input": batch, "model": self._config.model},
        )
        if resp.status_code == 429:
            ra_val = parse_retry_after(resp.headers.get("retry-after"))
            raise RateLimitError(retry_after=ra_val)
        resp.raise_for_status()
        data = resp.json()["data"]
        data.sort(key=lambda x: x["index"])
        if len(data) != len(batch):
            # A short ``data`` array (endpoint dropping items under load) must
            # fail loudly rather than truncate the zip in the index engine and
            # poison the content-hash skip forever (issue #1563, same failure
            # class as OllamaEmbedder). Non-retryable by design.
            raise EmbeddingError(
                f"OpenAI endpoint returned {len(data)} embeddings for {len(batch)} inputs "
                f"(model={self._config.model!r}); refusing to truncate."
            )
        if [item["index"] for item in data] != list(range(len(batch))):
            # Right count, wrong indices — duplicate or non-contiguous ``index``
            # values (e.g. two records for index=0 and none for index=1). Since
            # we extract positionally after sorting, this silently maps the
            # wrong vector to a chunk, a mis-embedding the content-hash skip
            # would preserve permanently (issue #1563, alignment variant).
            raise EmbeddingError(
                f"OpenAI endpoint returned malformed embedding indices for "
                f"{len(batch)} inputs (model={self._config.model!r}); refusing "
                "a misaligned result."
            )
        return [item["embedding"] for item in data]

    async def embed_texts(
        self,
        texts: Sequence[str],
        *,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> list[list[float]]:
        import asyncio

        if not texts:
            return []

        bs = self._config.batch_size
        batches = [list(texts[i : i + bs]) for i in range(0, len(texts), bs)]
        sem = asyncio.Semaphore(self._config.max_concurrent_batches)
        # ``done`` is a single-cell list so the inner closure can mutate it
        # without ``nonlocal``. asyncio is single-threaded so the ``+=`` is
        # race-free across concurrent batch coroutines. ``done`` is monotonic
        # but does NOT track batch-position — concurrent batches finish in
        # arbitrary order; consumers should treat it as a count, not an index.
        done = [0]
        total = len(texts)
        progress_warned = [False]

        async def _safe_embed(batch: list[str]) -> list[list[float]]:
            async with sem:
                result = await self._embed_batch_with_retry(batch)
                done[0] += len(batch)
                if on_progress is not None:
                    try:
                        on_progress(done[0], total)
                    except Exception:
                        if not progress_warned[0]:
                            progress_warned[0] = True
                            logger.debug(
                                "on_progress raised; further failures silenced",
                                exc_info=True,
                            )
                return result

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
        except RateLimitError as exc:
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

    async def embed_query(self, query: str) -> list[float]:
        if not query or not query.strip():
            raise EmbeddingError("Query text cannot be empty")
        embeddings = await self.embed_texts([query])
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
