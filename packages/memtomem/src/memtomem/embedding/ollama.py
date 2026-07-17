"""Ollama embedding provider using httpx."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Sequence

import httpx

from memtomem.config import EmbeddingConfig
from memtomem.embedding.retry import RateLimitError, parse_retry_after, with_retry
from memtomem.errors import EmbeddingError

logger = logging.getLogger(__name__)


class OllamaEmbedder:
    # Remote HTTP embedding is latency-bound, so concurrent per-file
    # ``embed_texts`` calls genuinely overlap; match the index engine's
    # file-level cap to preserve pre-#1783 throughput. Intra-file batch
    # fan-out is still self-limited by ``max_concurrent_batches`` below.
    preferred_concurrency = 8

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
        retryable_exceptions=(httpx.ConnectError, httpx.TimeoutException, RateLimitError),
    )
    async def _embed_batch_with_retry(self, batch: list[str]) -> list[list[float]]:
        """Send a single batch to Ollama with retry on transient errors."""
        client = self._get_client()
        resp = await client.post(
            "/api/embed",
            json={"model": self._config.model, "input": batch},
        )
        if resp.status_code == 429 or resp.status_code >= 500:
            # Ollama commonly returns 503 while a model is (re)loading — a
            # transient state the backoff is meant for, same as the OpenAI
            # provider's 429 handling. ``raise_for_status`` alone would raise
            # ``HTTPStatusError``, which is not in the retry tuple, so the
            # first embed after an ``ollama serve`` restart aborted instead of
            # using the 3-attempt backoff (#1574 item 2). 4xx (e.g. 404
            # unknown model) stays terminal below.
            raise RateLimitError(
                retry_after=parse_retry_after(resp.headers.get("retry-after")),
                message=f"Ollama returned transient HTTP {resp.status_code}",
            )
        resp.raise_for_status()
        data = resp.json()
        embeddings = data.get("embeddings")
        if embeddings is None:
            raise EmbeddingError(
                f"Ollama API returned unexpected response (missing 'embeddings' key): {list(data.keys())}"
            )
        if len(embeddings) != len(batch):
            # A short array (flaky/OOM-pressured server) must be a hard error,
            # not silent truncation: the index engine ``zip``s these vectors
            # against chunks, so a dropped tail would land BM25-only rows whose
            # content_hash is still committed — poisoning the re-index skip
            # forever (issue #1563). EmbeddingError is non-retryable and aborts
            # the whole index pass with zero DB writes.
            raise EmbeddingError(
                f"Ollama returned {len(embeddings)} embeddings for {len(batch)} inputs "
                f"(model={self._config.model!r}); refusing to truncate."
            )
        return embeddings

    async def embed_texts(
        self,
        texts: Sequence[str],
        *,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> list[list[float]]:
        import asyncio

        bs = self._config.batch_size
        batches = [list(texts[i : i + bs]) for i in range(0, len(texts), bs)]
        sem = asyncio.Semaphore(self._config.max_concurrent_batches)
        # See ``openai.py:embed_texts`` for the rationale on the single-cell
        # ``done`` counter — same ordering caveat applies (count, not index).
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
        except RateLimitError as e:
            # Retries exhausted on 429/5xx — most commonly Ollama returning
            # 503 for the whole backoff window while a large model loads.
            raise EmbeddingError(
                f"Ollama kept returning a transient HTTP error after retries. "
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
