"""Automatic fact extraction from tool responses."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore[assignment]

from memtomem_stm.proxy.config import (
    ExtractionConfig,
    ExtractionStrategy,
    LLMProvider,
)
from memtomem_stm.utils.circuit_breaker import CircuitBreaker

logger = logging.getLogger(__name__)

# Regex to extract JSON array from markdown code blocks or raw text
_JSON_ARRAY_RE = re.compile(r"\[[\s\S]*?\]")


@dataclass(frozen=True, slots=True)
class ExtractedFact:
    """A single atomic fact extracted from a tool response."""

    content: str
    category: str
    confidence: float
    tags: list[str] = field(default_factory=list)


def _parse_facts_json(raw: str, *, max_facts: int) -> list[ExtractedFact]:
    """Parse LLM output into ExtractedFact list. Tolerant of markdown wrapping."""
    for candidate in (raw.strip(), *_JSON_ARRAY_RE.findall(raw)):
        try:
            data = json.loads(candidate)
            if isinstance(data, list):
                facts = []
                for item in data[:max_facts]:
                    if not isinstance(item, dict) or "content" not in item:
                        continue
                    facts.append(
                        ExtractedFact(
                            content=str(item["content"]).strip(),
                            category=str(item.get("category", "technical")),
                            confidence=float(item.get("confidence", 0.5)),
                            tags=[str(t) for t in item.get("tags", [])],
                        )
                    )
                if facts:
                    return facts
        except (json.JSONDecodeError, ValueError, TypeError):
            continue
    return []


def _extract_heuristic(text: str, *, max_facts: int) -> list[ExtractedFact]:
    """Heuristic fallback for fact extraction.

    STM no longer imports memtomem core directly; the LLM extractor is the
    primary path. This stub returns an empty list, so when LLM extraction
    fails (circuit open, transport error) extraction silently yields no facts.
    A native STM heuristic can be added later if demand emerges.
    """
    return []


class FactExtractor:
    """Extract discrete facts from tool responses using LLM or heuristic.

    Follows the same patterns as LLMCompressor: httpx.AsyncClient,
    CircuitBreaker, multi-provider support, graceful fallback.
    """

    _OPENAI_URL = "https://api.openai.com/v1/chat/completions"
    _ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
    _ANTHROPIC_VERSION = "2023-06-01"

    def __init__(self, config: ExtractionConfig) -> None:
        self._cfg = config
        self._llm_cfg = config.effective_llm()
        self._cb = CircuitBreaker(
            max_failures=3,
            reset_timeout=60.0,
            name=f"extraction-{self._llm_cfg.provider.value}",
        )
        self._client: httpx.AsyncClient | None = httpx.AsyncClient(timeout=30) if httpx else None

    async def extract(
        self,
        text: str,
        *,
        server: str,
        tool: str,
    ) -> list[ExtractedFact]:
        """Extract facts from text. Returns empty list on failure."""
        if not text or len(text) < self._cfg.min_response_chars:
            return []

        truncated = text[: self._cfg.max_input_chars]

        strategy = self._cfg.strategy
        if strategy == ExtractionStrategy.NONE:
            return []
        if strategy == ExtractionStrategy.HEURISTIC:
            return _extract_heuristic(truncated, max_facts=self._cfg.max_facts)
        if strategy == ExtractionStrategy.HYBRID:
            return await self._extract_hybrid(truncated, server=server, tool=tool)

        # LLM strategy
        return await self._extract_llm(truncated, server=server, tool=tool)

    async def _extract_llm(self, text: str, *, server: str, tool: str) -> list[ExtractedFact]:
        """LLM-based extraction with circuit breaker and fallback."""
        if self._client is None:
            return _extract_heuristic(text, max_facts=self._cfg.max_facts)
        if self._cb.is_open:
            logger.debug("Extraction circuit open, falling back to heuristic")
            return _extract_heuristic(text, max_facts=self._cfg.max_facts)
        try:
            raw = await self._call_api(text)
            self._cb.record_success()
            facts = _parse_facts_json(raw, max_facts=self._cfg.max_facts)
            if not facts:
                logger.debug("LLM returned no parseable facts for %s/%s", server, tool)
            return facts
        except Exception as exc:
            self._cb.record_failure()
            logger.warning(
                "LLM extraction failed (%s) for %s/%s, falling back to heuristic: %s",
                type(exc).__name__,
                server,
                tool,
                exc,
            )
            return _extract_heuristic(text, max_facts=self._cfg.max_facts)

    async def _extract_hybrid(self, text: str, *, server: str, tool: str) -> list[ExtractedFact]:
        """Combine LLM + heuristic extraction, deduplicate by content."""
        llm_facts = await self._extract_llm(text, server=server, tool=tool)
        heuristic_facts = _extract_heuristic(text, max_facts=self._cfg.max_facts)

        seen: set[str] = {f.content.lower() for f in llm_facts}
        merged = list(llm_facts)
        for f in heuristic_facts:
            if f.content.lower() not in seen:
                seen.add(f.content.lower())
                merged.append(f)
        return merged[: self._cfg.max_facts]

    async def _call_api(self, text: str) -> str:
        system_prompt = self._llm_cfg.system_prompt.format(max_facts=self._cfg.max_facts)
        match self._llm_cfg.provider:
            case LLMProvider.OPENAI:
                return await self._openai(text, system_prompt)
            case LLMProvider.ANTHROPIC:
                return await self._anthropic(text, system_prompt)
            case LLMProvider.OLLAMA:
                return await self._ollama(text, system_prompt)

    async def _openai(self, text: str, system_prompt: str) -> str:
        url = (
            self._llm_cfg.base_url.rstrip("/") + "/v1/chat/completions"
            if self._llm_cfg.base_url
            else self._OPENAI_URL
        )
        resp = await self._client.post(
            url,
            headers={"Authorization": f"Bearer {self._llm_cfg.api_key}"},
            json={
                "model": self._llm_cfg.model,
                "max_tokens": self._llm_cfg.max_tokens,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": text},
                ],
            },
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    async def _anthropic(self, text: str, system_prompt: str) -> str:
        url = (
            self._llm_cfg.base_url.rstrip("/") + "/v1/messages"
            if self._llm_cfg.base_url
            else self._ANTHROPIC_URL
        )
        resp = await self._client.post(
            url,
            headers={
                "x-api-key": self._llm_cfg.api_key,
                "anthropic-version": self._ANTHROPIC_VERSION,
            },
            json={
                "model": self._llm_cfg.model,
                "max_tokens": self._llm_cfg.max_tokens,
                "system": system_prompt,
                "messages": [{"role": "user", "content": text}],
            },
        )
        resp.raise_for_status()
        return resp.json()["content"][0]["text"]

    async def _ollama(self, text: str, system_prompt: str) -> str:
        base = self._llm_cfg.base_url or "http://localhost:11434"
        url = base.rstrip("/") + "/api/chat"
        resp = await self._client.post(
            url,
            json={
                "model": self._llm_cfg.model,
                "stream": False,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": text},
                ],
            },
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()["message"]["content"]

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None
