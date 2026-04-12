"""Ollama LLM provider using httpx."""

from __future__ import annotations

import logging

import httpx

from memtomem.config import LLMConfig
from memtomem.embedding.retry import with_retry
from memtomem.errors import LLMError

logger = logging.getLogger(__name__)


class OllamaLLM:
    def __init__(self, config: LLMConfig) -> None:
        self._config = config
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._config.base_url,
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
            "prompt": prompt,
            "stream": False,
            "options": {"num_predict": max_tokens},
        }
        if system:
            payload["system"] = system
        resp = await client.post("/api/generate", json=payload)
        resp.raise_for_status()
        data = resp.json()
        response_text = data.get("response")
        if response_text is None:
            raise LLMError(
                f"Ollama API returned unexpected response (missing 'response' key): "
                f"{list(data.keys())}"
            )
        return response_text

    async def generate(self, prompt: str, *, system: str = "", max_tokens: int = 1024) -> str:
        try:
            return await self._generate_with_retry(prompt, system=system, max_tokens=max_tokens)
        except httpx.ConnectError as e:
            raise LLMError(
                f"Cannot connect to Ollama at {self._config.base_url}. "
                f"Please verify 'ollama serve' is running. Error: {e}"
            ) from e
        except httpx.TimeoutException as e:
            raise LLMError(
                f"Ollama generation request timed out. "
                f"The model '{self._config.model}' may still be loading. Error: {e}"
            ) from e
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                raise LLMError(
                    f"Model '{self._config.model}' not found in Ollama. "
                    f"Run 'ollama pull {self._config.model}' to download it."
                ) from e
            raise LLMError(f"Ollama generation failed: {e}") from e
        except httpx.HTTPError as e:
            raise LLMError(f"Ollama generation failed: {e}") from e

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                logger.debug("Failed to close HTTP client", exc_info=True)
            self._client = None
