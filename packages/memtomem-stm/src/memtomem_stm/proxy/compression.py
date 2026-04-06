"""Response compression strategies."""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Protocol

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore[assignment]

from memtomem_stm.proxy.config import (
    CompressionStrategy,
    LLMCompressorConfig,
    LLMProvider,
    TailMode,
)
from memtomem_stm.utils.circuit_breaker import CircuitBreaker as _CircuitBreaker

logger = logging.getLogger(__name__)


def _content_summary(text: str) -> str:
    """Count structural elements in the original text for truncation metadata."""
    counts: list[str] = []
    headings = len(re.findall(r"(?:^|\n)#{1,6}\s", text))
    code_blocks = len(re.findall(r"```", text)) // 2
    list_items = len(re.findall(r"(?:^|\n)\s*[-*]\s", text))
    links = len(re.findall(r"\[.*?\]\(.*?\)", text))
    if headings:
        counts.append(f"{headings} headings")
    if code_blocks:
        counts.append(f"{code_blocks} code blocks")
    if list_items:
        counts.append(f"{list_items} list items")
    if links:
        counts.append(f"{links} links")
    return f" [{', '.join(counts)}]" if counts else ""


class Compressor(Protocol):
    def compress(self, text: str, *, max_chars: int) -> str: ...


class NoopCompressor:
    """No compression — passthrough."""

    def compress(self, text: str, *, max_chars: int) -> str:
        return text


class TruncateCompressor:
    """Character limit with sentence/word boundary awareness.

    For text with markdown headings, prefers to cut at heading boundaries
    and appends a list of remaining section titles. For plain text, cuts
    at the nearest sentence or word boundary.
    """

    _HEADING_RE = re.compile(r"(?:^|\n)(#{1,6}\s+.+)")

    def compress(self, text: str, *, max_chars: int) -> str:
        if not text or len(text) <= max_chars:
            return text

        # Try section-aware truncation for markdown with headings
        headings = list(self._HEADING_RE.finditer(text))
        if len(headings) >= 2:
            return self._section_aware_truncate(text, max_chars, headings)

        # Fallback: position-based truncation
        break_at = self._find_break(text, max_chars)
        summary = _content_summary(text)
        return text[:break_at] + f"\n... (truncated, original: {len(text)} chars){summary}"

    _SUMMARY_RE = re.compile(r"summary|conclusion|결론|요약", re.IGNORECASE)

    def _section_aware_truncate(
        self, text: str, max_chars: int, headings: list[re.Match]
    ) -> str:
        """Cut at the last complete heading section that fits within budget.

        If the last section is a summary/conclusion, it is appended to the
        kept portion (budget permitting) so critical wrap-up info is preserved.
        Remaining section titles are listed in the footer.
        """
        # Reserve space for the footer (section list + truncation notice)
        footer_budget = min(500, max_chars // 4)
        content_budget = max_chars - footer_budget

        # Check if the last section is a summary/conclusion
        summary_text = ""
        last_title = headings[-1].group(1).strip() if headings else ""
        if self._SUMMARY_RE.search(last_title):
            summary_start = headings[-1].start()
            summary_text = text[summary_start:].rstrip()
            # Reserve budget for the summary section
            summary_budget = min(len(summary_text), content_budget // 3)
            content_budget -= summary_budget

        # Find the last heading whose START fits within content_budget
        last_fit_idx = 0
        for i, m in enumerate(headings):
            if m.start() <= content_budget:
                last_fit_idx = i
            else:
                break

        # Include at least the first heading's content
        if last_fit_idx + 1 < len(headings):
            cut_at = headings[last_fit_idx + 1].start()
        else:
            cut_at = self._find_break(text, content_budget)

        kept = text[:cut_at].rstrip()

        # Build footer with remaining section titles
        remaining_titles = []
        for m in headings[last_fit_idx + 1 :]:
            title = m.group(1).strip()
            # Don't list the summary section — it's appended below
            if summary_text and m.start() == headings[-1].start():
                continue
            remaining_titles.append(title)

        if remaining_titles:
            titles_str = ", ".join(
                t.lstrip("#").strip() for t in remaining_titles
            )
            footer = (
                f"\n\n... ({len(remaining_titles)} more sections: {titles_str})"
                f"\n(truncated, original: {len(text)} chars)"
            )
        else:
            footer = f"\n... (truncated, original: {len(text)} chars)"

        result = kept + footer

        # Append summary section if detected and budget allows
        if summary_text and headings[-1].start() > cut_at:
            budget_left = max_chars - len(result)
            if budget_left >= 100:
                if len(summary_text) <= budget_left:
                    result += "\n\n" + summary_text
                else:
                    result += "\n\n" + summary_text[: budget_left - 20] + "\n... (summary truncated)"

        if len(result) > max_chars:
            result = result[:max_chars]
        return result

    @staticmethod
    def _find_break(text: str, max_chars: int) -> int:
        end = min(max_chars, len(text) - 1)
        floor = int(max_chars * 0.8)
        for i in range(end, floor - 1, -1):
            if text[i - 1] in ".!?\n" and (i >= len(text) or text[i] in " \n\t"):
                return i
        for i in range(end, floor - 1, -1):
            if i < len(text) and text[i] in " \n\t":
                return i
        return max_chars


@dataclass
class PendingSelection:
    """Stores original chunks while waiting for section selection."""

    chunks: dict[str, str]
    format: str
    created_at: float
    total_chars: int


class SelectiveCompressor:
    """2-phase compression: Phase 1 returns a TOC, Phase 2 returns selected sections."""

    def __init__(
        self,
        max_pending: int = 100,
        pending_ttl_seconds: float = 300.0,
        json_depth: int = 1,
        min_section_chars: int = 50,
    ) -> None:
        self._max_pending = max_pending
        self._ttl = pending_ttl_seconds
        self._json_depth = json_depth
        self._min_section_chars = min_section_chars
        self._pending: dict[str, PendingSelection] = {}
        self._insertion_order: deque[str] = deque()

    def compress(self, text: str, *, max_chars: int) -> str:
        if not text or len(text) <= max_chars:
            return text

        fmt, chunks = self._detect_and_parse(text)

        if len(chunks) <= 1:
            chunks = self._decompose_single_chunk(chunks, fmt)
            if len(chunks) <= 1:
                return TruncateCompressor().compress(text, max_chars=max_chars)

        return self._store_and_build_toc(text, fmt, chunks)

    def compress_full_toc(self, text: str, *, max_chars: int) -> str | None:
        fmt, chunks = self._detect_and_parse(text)
        if len(chunks) <= 1:
            chunks = self._decompose_single_chunk(chunks, fmt)
            if len(chunks) <= 1:
                return None
        return self._store_and_build_toc(text, fmt, chunks)

    def _store_and_build_toc(self, text: str, fmt: str, chunks: dict[str, str]) -> str:
        selection_key = uuid.uuid4().hex[:12]

        self._pending[selection_key] = PendingSelection(
            chunks=chunks,
            format=fmt,
            created_at=time.monotonic(),
            total_chars=len(text),
        )
        self._insertion_order.append(selection_key)
        self._evict()

        entries = []
        for key, content in chunks.items():
            size = len(content)
            is_inline = size < self._min_section_chars
            preview = content if is_inline else content[:80].replace("\n", " ")
            content_type = self._infer_type(key, content, fmt)
            entries.append(
                {
                    "key": key,
                    "type": content_type,
                    "size": size,
                    "preview": preview,
                    "inline": is_inline,
                }
            )

        toc = {
            "type": "toc",
            "selection_key": selection_key,
            "format": fmt,
            "total_chars": len(text),
            "entries": entries,
            "hint": f"Call stm_proxy_select_chunks(key='{selection_key}', sections=[...]) to retrieve.",
        }
        return json.dumps(toc, ensure_ascii=False)

    def select(self, key: str, sections: list[str]) -> str:
        self._evict_expired()

        pending = self._pending.get(key)
        if pending is None:
            return f"Selection key '{key}' not found or expired."

        pending.created_at = time.monotonic()

        selected_parts: list[str] = []
        for section in sections:
            if section in pending.chunks:
                selected_parts.append(pending.chunks[section])

        if not selected_parts:
            available = list(pending.chunks.keys())
            return f"No matching sections found. Available: {available}"

        return "\n\n".join(selected_parts)

    def _detect_and_parse(self, text: str) -> tuple[str, dict[str, str]]:
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                return "json", self._parse_json_dict(data, text)
            if isinstance(data, list):
                return "json", self._parse_json_array(data)
        except (json.JSONDecodeError, ValueError):
            pass

        if re.search(r"(?:^|\n)#{1,6}\s", text):
            return "markdown", self._parse_markdown(text)

        return "text", self._parse_text(text)

    def _parse_json_dict(
        self, data: dict[str, object], raw_text: str, prefix: str = "", depth: int = 0
    ) -> dict[str, str]:
        chunks: dict[str, str] = {}
        for key, value in data.items():
            full_key = f"{prefix}{key}" if not prefix else f"{prefix}.{key}"
            if isinstance(value, dict) and depth < self._json_depth:
                nested = self._parse_json_dict(value, "", prefix=full_key, depth=depth + 1)
                chunks.update(nested)
            else:
                chunks[full_key] = json.dumps(value, ensure_ascii=False, indent=2)
        return chunks

    def _parse_json_array(self, data: list[object]) -> dict[str, str]:
        chunks: dict[str, str] = {}
        for i, item in enumerate(data):
            chunks[f"[{i}]"] = json.dumps(item, ensure_ascii=False, indent=2)
        return chunks

    def _parse_markdown(self, text: str) -> dict[str, str]:
        chunks: dict[str, str] = {}
        parts = re.split(r"(?:^|\n)(#{1,6}\s+.+)", text)
        current_heading = ""
        current_content: list[str] = []

        for part in parts:
            heading_match = re.match(r"^(#{1,6})\s+(.+)$", part.strip())
            if heading_match:
                if current_heading or current_content:
                    content = "\n".join(current_content).strip()
                    if content:
                        chunks[current_heading or "Preamble"] = content
                current_heading = heading_match.group(2).strip()
                current_content = []
            else:
                current_content.append(part)

        if current_heading or current_content:
            content = "\n".join(current_content).strip()
            if content:
                chunks[current_heading or "Preamble"] = content

        return chunks

    def _parse_text(self, text: str) -> dict[str, str]:
        paragraphs = re.split(r"\n\n+", text)
        chunks: dict[str, str] = {}
        for i, para in enumerate(paragraphs):
            stripped = para.strip()
            if stripped:
                chunks[f"Paragraph {i + 1}"] = stripped
        return chunks

    def _decompose_single_chunk(self, chunks: dict[str, str], fmt: str) -> dict[str, str]:
        if not chunks:
            return chunks
        key, value = next(iter(chunks.items()))
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return chunks
        if isinstance(parsed, dict) and len(parsed) > 1:
            return {
                f"{key}.{k}": json.dumps(v, ensure_ascii=False, indent=2) for k, v in parsed.items()
            }
        if isinstance(parsed, list) and len(parsed) > 1:
            return {
                f"{key}[{i}]": json.dumps(item, ensure_ascii=False, indent=2)
                for i, item in enumerate(parsed)
            }
        return chunks

    def _infer_type(self, key: str, content: str, fmt: str) -> str:
        if fmt == "json":
            if content.startswith("{"):
                return "object"
            if content.startswith("["):
                return "array"
            return "string"
        if fmt == "markdown":
            return "heading"
        return "paragraph"

    def _evict(self) -> None:
        self._evict_expired()
        while len(self._pending) > self._max_pending and self._insertion_order:
            oldest_key = self._insertion_order.popleft()
            self._pending.pop(oldest_key, None)

    def _evict_expired(self) -> None:
        now = time.monotonic()
        expired = {k for k, v in self._pending.items() if (now - v.created_at) > self._ttl}
        for k in expired:
            self._pending.pop(k, None)
        if expired:
            self._insertion_order = deque(k for k in self._insertion_order if k not in expired)


class FieldExtractCompressor:
    """JSON: preserve key structure + truncate values. Text: head + tail lines."""

    def compress(self, text: str, *, max_chars: int) -> str:
        if not text or len(text) <= max_chars:
            return text
        try:
            data = json.loads(text)
            return self._compress_json(data, max_chars)
        except (json.JSONDecodeError, ValueError):
            pass
        return self._compress_text(text, max_chars)

    def _compress_json(self, data: object, max_chars: int) -> str:
        if isinstance(data, dict):
            summary = {}
            for key, value in data.items():
                if isinstance(value, str) and len(value) > 80:
                    if "```" in value:
                        limit = min(len(value), 500)
                        fence_end = value.rfind("```", 0, limit)
                        summary[key] = (
                            value[: fence_end + 3] if fence_end > 80 else value[:limit] + "..."
                        )
                    else:
                        summary[key] = value[:80] + "..."
                elif isinstance(value, list):
                    preview_n = min(3, len(value))
                    preview: list[object] = []
                    for item in value[:preview_n]:
                        if isinstance(item, str) and len(item) > 60:
                            preview.append(item[:60] + "...")
                        elif isinstance(item, dict):
                            preview.append(self._preview_dict(item))
                        elif isinstance(item, list):
                            preview.append(f"[{len(item)} items]")
                        else:
                            preview.append(item)
                    remaining = len(value) - preview_n
                    if remaining > 0:
                        preview.append(f"... ({remaining} more)")
                    summary[key] = preview
                elif isinstance(value, dict):
                    summary[key] = self._preview_dict(value)
                else:
                    summary[key] = value
            result = json.dumps(summary, ensure_ascii=False, indent=2)
        elif isinstance(data, list):
            preview = data[:3]
            result = json.dumps(preview, ensure_ascii=False, indent=2)
            if len(data) > 3:
                result += f"\n... ({len(data)} items total, showing first 3)"
        else:
            result = json.dumps(data, ensure_ascii=False)
        if len(result) > max_chars:
            result = result[:max_chars] + "\n... (truncated)"
        return result

    @staticmethod
    def _preview_dict(d: dict, max_keys: int = 2, max_value_len: int = 40) -> dict:
        """Show first N key-value pairs with truncated values, hint at rest."""
        preview = {}
        keys = list(d.keys())
        for k in keys[:max_keys]:
            v = d[k]
            if isinstance(v, str) and len(v) > max_value_len:
                preview[k] = v[:max_value_len] + "..."
            elif isinstance(v, dict):
                preview[k] = f"{{{len(v)} keys}}"
            elif isinstance(v, list):
                preview[k] = f"[{len(v)} items]"
            else:
                preview[k] = v
        remaining = len(keys) - max_keys
        if remaining > 0:
            preview[f"...{remaining} more"] = "..."
        return preview

    def _compress_text(self, text: str, max_chars: int) -> str:
        lines = text.split("\n")
        if len(lines) <= 10:
            return text[:max_chars] + "\n... (truncated)" if len(text) > max_chars else text
        head_count = max(3, len(lines) // 10)
        tail_count = max(3, len(lines) // 10)
        head = "\n".join(lines[:head_count])
        tail = "\n".join(lines[-tail_count:])
        omitted = len(lines) - head_count - tail_count
        summary = _content_summary(text)
        result = f"{head}\n... ({omitted} lines omitted){summary} ...\n{tail}"
        if len(result) > max_chars:
            result = result[:max_chars] + "\n... (truncated)"
        return result


class LLMCompressor:
    """Compress by asking an LLM to summarize the text."""

    _OPENAI_URL = "https://api.openai.com/v1/chat/completions"
    _ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
    _ANTHROPIC_VERSION = "2023-06-01"

    def __init__(self, config: LLMCompressorConfig) -> None:
        self._cfg = config
        self._cb = _CircuitBreaker(
            max_failures=3, reset_timeout=60.0, name=f"llm-{config.provider.value}"
        )
        self._client: httpx.AsyncClient | None = httpx.AsyncClient(timeout=30) if httpx else None

    async def compress(
        self, text: str, *, max_chars: int, privacy_patterns: list[str] | None = None
    ) -> str:
        if not text or len(text) <= max_chars:
            return text
        if privacy_patterns:
            from memtomem_stm.proxy.privacy import contains_sensitive_content

            if contains_sensitive_content(text, privacy_patterns):
                logger.info("Sensitive content detected, skipping LLM compression")
                return TruncateCompressor().compress(text, max_chars=max_chars)
        if self._cb.is_open:
            return TruncateCompressor().compress(text, max_chars=max_chars)
        try:
            result = await self._call_api(text, max_chars=max_chars)
            self._cb.success()
            return result
        except Exception as exc:
            self._cb.failure()
            logger.warning(
                "LLM compression failed (%s), falling back to truncate: %s", type(exc).__name__, exc
            )
            return TruncateCompressor().compress(text, max_chars=max_chars)

    async def _call_api(self, text: str, *, max_chars: int) -> str:
        system_prompt = self._cfg.system_prompt.format(max_chars=max_chars)
        match self._cfg.provider:
            case LLMProvider.OPENAI:
                return await self._openai(text, system_prompt)
            case LLMProvider.ANTHROPIC:
                return await self._anthropic(text, system_prompt)
            case LLMProvider.OLLAMA:
                return await self._ollama(text, system_prompt)

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _openai(self, text: str, system_prompt: str) -> str:
        url = (
            self._cfg.base_url.rstrip("/") + "/v1/chat/completions"
            if self._cfg.base_url
            else self._OPENAI_URL
        )
        resp = await self._client.post(
            url,
            headers={"Authorization": f"Bearer {self._cfg.api_key}"},
            json={
                "model": self._cfg.model,
                "max_tokens": self._cfg.max_tokens,
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
            self._cfg.base_url.rstrip("/") + "/v1/messages"
            if self._cfg.base_url
            else self._ANTHROPIC_URL
        )
        resp = await self._client.post(
            url,
            headers={
                "x-api-key": self._cfg.api_key,
                "anthropic-version": self._ANTHROPIC_VERSION,
            },
            json={
                "model": self._cfg.model,
                "max_tokens": self._cfg.max_tokens,
                "system": system_prompt,
                "messages": [{"role": "user", "content": text}],
            },
        )
        resp.raise_for_status()
        return resp.json()["content"][0]["text"]

    async def _ollama(self, text: str, system_prompt: str) -> str:
        base = self._cfg.base_url or "http://localhost:11434"
        url = base.rstrip("/") + "/api/chat"
        resp = await self._client.post(
            url,
            json={
                "model": self._cfg.model,
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


class HybridCompressor:
    """Head preserve + tail compress (TOC or truncate)."""

    _SEPARATOR_TEMPLATE = "\n---\nRemaining content ({remaining} chars) — Table of Contents:\n\n"
    _SEPARATOR_TRUNC_TEMPLATE = "\n---\nRemaining content ({remaining} chars, truncated):\n\n"

    def __init__(
        self,
        head_chars: int = 5000,
        tail_mode: TailMode = TailMode.TOC,
        min_toc_budget: int = 200,
        min_head_chars: int = 100,
        head_ratio: float = 0.6,
        selective_compressor: SelectiveCompressor | None = None,
    ) -> None:
        self._head_chars = head_chars
        self._tail_mode = tail_mode
        self._min_toc_budget = min_toc_budget
        self._min_head_chars = min_head_chars
        self._head_ratio = head_ratio
        self._selective = selective_compressor or SelectiveCompressor()

    @property
    def selective_compressor(self) -> SelectiveCompressor:
        return self._selective

    def compress(self, text: str, *, max_chars: int) -> str:
        if not text or len(text) <= max_chars:
            return text
        if len(text) <= self._head_chars:
            return text

        separator_overhead = 80
        _MIN_TAIL = 50
        available = max_chars - separator_overhead

        if available <= 0:
            return TruncateCompressor().compress(text, max_chars=max_chars)

        if self._head_chars + self._min_toc_budget <= available:
            head_budget = self._head_chars
        else:
            head_budget = max(self._min_head_chars, int(available * self._head_ratio))

        if head_budget > available or head_budget < self._min_head_chars:
            return TruncateCompressor().compress(text, max_chars=max_chars)

        if available - head_budget < _MIN_TAIL:
            return TruncateCompressor().compress(text, max_chars=max_chars)

        head_end = self._find_head_break(text, head_budget)
        head = text[:head_end]
        tail_text = text[head_end:]
        remaining = len(tail_text)

        if self._tail_mode == TailMode.TOC:
            separator = self._SEPARATOR_TEMPLATE.format(remaining=remaining)
        else:
            separator = self._SEPARATOR_TRUNC_TEMPLATE.format(remaining=remaining)

        toc_budget = max_chars - len(head) - len(separator)
        if toc_budget < _MIN_TAIL:
            return TruncateCompressor().compress(text, max_chars=max_chars)

        if self._tail_mode == TailMode.TOC:
            tail_compressed = self._selective.compress(tail_text, max_chars=toc_budget)
        else:
            tail_compressed = TruncateCompressor().compress(tail_text, max_chars=toc_budget)

        result = head + separator + tail_compressed
        if len(result) > max_chars:
            result = result[:max_chars]
        return result

    def _find_head_break(self, text: str, budget: int) -> int:
        floor = int(budget * 0.85)
        for i in range(budget, floor - 1, -1):
            if i + 1 < len(text) and text[i : i + 2] == "\n\n":
                return i
            if i >= 2 and text[i - 2 : i] == "\n\n":
                return i - 2
        for i in range(budget, floor - 1, -1):
            if text[i - 1] in ".!?\n" and (i >= len(text) or text[i] in " \n\t"):
                return i
        for i in range(budget, floor - 1, -1):
            if i < len(text) and text[i] in " \n\t":
                return i
        return budget


def get_compressor(strategy: CompressionStrategy) -> Compressor:
    """Factory for sync compressor instances (excludes LLM_SUMMARY, SELECTIVE, HYBRID)."""
    match strategy:
        case CompressionStrategy.NONE:
            return NoopCompressor()
        case CompressionStrategy.TRUNCATE:
            return TruncateCompressor()
        case CompressionStrategy.EXTRACT_FIELDS:
            return FieldExtractCompressor()
        case _:
            return TruncateCompressor()


def auto_select_strategy(text: str) -> CompressionStrategy:
    """Detect content type and return the best compression strategy.

    - JSON → EXTRACT_FIELDS (preserves key structure)
    - Markdown with headings → HYBRID (head preserve + tail TOC)
    - Plain text → TRUNCATE (sentence-aware)
    """
    stripped = text.strip()
    if not stripped:
        return CompressionStrategy.NONE

    # JSON detection
    if stripped[0] in "{[":
        try:
            json.loads(stripped)
            return CompressionStrategy.EXTRACT_FIELDS
        except (json.JSONDecodeError, ValueError):
            pass

    # Markdown detection (multiple headings → hybrid benefits from TOC)
    heading_count = len(re.findall(r"(?:^|\n)#{1,6}\s", stripped))
    if heading_count >= 3:
        return CompressionStrategy.HYBRID

    # Code-heavy content (multiple code fences → preserve head)
    fence_count = stripped.count("```")
    if fence_count >= 4:  # 2+ code blocks
        return CompressionStrategy.HYBRID

    return CompressionStrategy.TRUNCATE
