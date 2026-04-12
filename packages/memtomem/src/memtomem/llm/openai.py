"""OpenAI-compatible LLM provider using httpx."""

from __future__ import annotations

import logging

import httpx

from memtomem.config import LLMConfig
from memtomem.embedding.retry import with_retry
from memtomem.errors import LLMError

logger = logging.getLogger(__name__)


class OpenAILLM:
    def __init__(self, config: LLMConfig) -> None:
        self._config = config
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            headers: dict[str, str] = {}
            if self._config.api_key:
                headers["Authorization"] = f"Bearer {self._config.api_key}"
            self._client = httpx.AsyncClient(
                base_url=self._config.base_url,
                headers=headers,
                timeout=self._config.timeout,
            )
        return self._client

    @with_retry(
        max_attempts=3,
        base_delay=1.0,
        retryable_exceptions=(httpx.ConnectError, httpx.TimeoutException),
    )
    async def _generate_with_retry(self, prompt: str, *, system: str, max_tokens: int) -> str:
        client = self._get_client()
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": self._config.model,
                "messages": messages,
                "max_tokens": max_tokens,
            },
        )
        if resp.status_code == 429:
            raise LLMError(
                f"OpenAI rate limit exceeded. "
                f"Retry-After: {resp.headers.get('Retry-After', 'unknown')}"
            )
        if resp.status_code == 401:
            raise LLMError("OpenAI authentication failed. Check your API key.")
        resp.raise_for_status()
        data = resp.json()
        choices = data.get("choices")
        if not choices:
            raise LLMError(
                f"OpenAI API returned unexpected response (no choices): {list(data.keys())}"
            )
        return choices[0]["message"]["content"]

    async def generate(self, prompt: str, *, system: str = "", max_tokens: int = 1024) -> str:
        try:
            return await self._generate_with_retry(prompt, system=system, max_tokens=max_tokens)
        except httpx.ConnectError as e:
            raise LLMError(
                f"Cannot connect to OpenAI at {self._config.base_url}. Error: {e}"
            ) from e
        except httpx.TimeoutException as e:
            raise LLMError(f"OpenAI request timed out. Error: {e}") from e
        except httpx.HTTPError as e:
            raise LLMError(f"OpenAI generation failed: {e}") from e

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                logger.debug("Failed to close HTTP client", exc_info=True)
            self._client = None
