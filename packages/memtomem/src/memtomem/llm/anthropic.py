"""Anthropic LLM provider using httpx."""

from __future__ import annotations

import logging

import httpx

from memtomem.config import LLMConfig
from memtomem.embedding.retry import with_retry
from memtomem.errors import LLMError

logger = logging.getLogger(__name__)

# Hardcoded — users should never change this; incorrect values break the API.
_ANTHROPIC_VERSION = "2023-06-01"
_DEFAULT_BASE_URL = "https://api.anthropic.com"


class AnthropicLLM:
    def __init__(self, config: LLMConfig) -> None:
        self._config = config
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            base_url = self._config.base_url
            # Use Anthropic's default when base_url points at Ollama's default
            if base_url == "http://localhost:11434":
                base_url = _DEFAULT_BASE_URL
            self._client = httpx.AsyncClient(
                base_url=base_url,
                headers={
                    "x-api-key": self._config.api_key,
                    "anthropic-version": _ANTHROPIC_VERSION,
                    "content-type": "application/json",
                },
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
        payload: dict = {
            "model": self._config.model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            payload["system"] = system
        resp = await client.post("/v1/messages", json=payload)
        if resp.status_code == 429:
            raise LLMError(
                f"Anthropic rate limit exceeded. "
                f"Retry-After: {resp.headers.get('Retry-After', 'unknown')}"
            )
        if resp.status_code == 401:
            raise LLMError("Anthropic authentication failed. Check your API key.")
        resp.raise_for_status()
        data = resp.json()
        content = data.get("content")
        if not content:
            raise LLMError(
                f"Anthropic API returned unexpected response (no content): {list(data.keys())}"
            )
        return content[0]["text"]

    async def generate(self, prompt: str, *, system: str = "", max_tokens: int = 1024) -> str:
        try:
            return await self._generate_with_retry(prompt, system=system, max_tokens=max_tokens)
        except httpx.ConnectError as e:
            raise LLMError(
                f"Cannot connect to Anthropic at {self._config.base_url}. Error: {e}"
            ) from e
        except httpx.TimeoutException as e:
            raise LLMError(f"Anthropic request timed out. Error: {e}") from e
        except httpx.HTTPError as e:
            raise LLMError(f"Anthropic generation failed: {e}") from e

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                logger.debug("Failed to close HTTP client", exc_info=True)
            self._client = None
