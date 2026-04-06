"""Content cleaning — noise removal before compression and indexing."""

from __future__ import annotations

import re
from typing import Protocol

_CODE_FENCE_RE = re.compile(r"(```[\s\S]*?```|`[^`\n]+`)")
_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>[\s\S]*?</\1>", re.I)
_HTML_TAG_RE = re.compile(r"<[a-zA-Z][\w.-]*(?:\s[^>]*)?\s*/?>")
_CLOSE_TAG_RE = re.compile(r"</[a-zA-Z][\w.-]*\s*>")
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")
_LINK_LINE_RE = re.compile(r"^\s*[-*]\s*\[.*?\]\(https?://\S+\)")
_BARE_URL_LINE_RE = re.compile(r"^\s*[-*]?\s*https?://\S+\s*$")
_GENERIC_RE = re.compile(r"[A-Z]\w*<[^>]+>")


class ContentCleaner(Protocol):
    def clean(self, text: str) -> str: ...


class DefaultContentCleaner:
    def __init__(self, config: object | None = None) -> None:
        # Accept a CleaningConfig (or any object with strip_html/deduplicate/collapse_links)
        self._strip_html = getattr(config, "strip_html", True) if config else True
        self._dedup = getattr(config, "deduplicate", True) if config else True
        self._collapse = getattr(config, "collapse_links", True) if config else True

    def clean(self, text: str) -> str:
        if not text:
            return text
        if self._strip_html:
            text = self._strip_html_jsx(text)
        if self._dedup:
            text = self._deduplicate_paragraphs(text)
        if self._collapse:
            text = self._collapse_link_floods(text)
        text = self._normalize_whitespace(text)
        return text.strip()

    def _strip_html_jsx(self, text: str) -> str:
        fences: list[str] = []

        def _save_fence(m: re.Match) -> str:
            fences.append(m.group(0))
            return f"\x00FENCE{len(fences) - 1}\x00"

        text = _CODE_FENCE_RE.sub(_save_fence, text)

        generics: list[str] = []

        def _save_generic(m: re.Match) -> str:
            generics.append(m.group(0))
            return f"\x00GEN{len(generics) - 1}\x00"

        text = _GENERIC_RE.sub(_save_generic, text)
        # Remove <script>/<style> blocks entirely (content + tags)
        text = _SCRIPT_STYLE_RE.sub("", text)
        text = _HTML_TAG_RE.sub("", text)
        text = _CLOSE_TAG_RE.sub("", text)

        for i, g in enumerate(generics):
            text = text.replace(f"\x00GEN{i}\x00", g)
        for i, f in enumerate(fences):
            text = text.replace(f"\x00FENCE{i}\x00", f)

        return text

    def _deduplicate_paragraphs(self, text: str) -> str:
        paragraphs = re.split(r"\n{2,}", text)
        seen: set[str] = set()
        unique: list[str] = []
        for p in paragraphs:
            normalized = " ".join(p.split())
            if normalized in seen:
                continue
            seen.add(normalized)
            unique.append(p)
        return "\n\n".join(unique)

    def _collapse_link_floods(self, text: str) -> str:
        paragraphs = re.split(r"\n{2,}", text)
        result: list[str] = []
        for p in paragraphs:
            lines = p.strip().split("\n")
            if len(lines) >= 10:
                link_count = sum(
                    1 for ln in lines if _LINK_LINE_RE.match(ln) or _BARE_URL_LINE_RE.match(ln)
                )
                if link_count / len(lines) >= 0.8:
                    result.append(f"[{link_count} links omitted]")
                    continue
            result.append(p)
        return "\n\n".join(result)

    def _normalize_whitespace(self, text: str) -> str:
        return _MULTI_NEWLINE_RE.sub("\n\n", text)
