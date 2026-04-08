"""Progressive (cursor-based) delivery for large tool responses.

Instead of lossy compression, stores the full cleaned content and delivers
it in chunks on demand — like Claude Code reads files 150 lines at a time.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from memtomem_stm.proxy.compression import PendingSelection

_HEADING_RE = re.compile(r"(?:^|\n)#{1,6}\s+(.+)")


@dataclass
class ProgressiveResponse:
    """Stored full content for progressive delivery."""

    content: str
    total_chars: int
    total_lines: int
    content_type: str  # "json" | "markdown" | "text" | "code"
    structure_hint: str  # e.g. "5 headings, 3 code blocks"
    created_at: float
    access_count: int = 0


class ProgressiveStoreAdapter:
    """Adapts PendingStore for progressive delivery storage.

    Reuses the existing PendingStore (InMemory or SQLite) without changes
    by encoding ProgressiveResponse into PendingSelection with format="progressive".
    """

    def __init__(self, store: object) -> None:
        self._store = store

    def put(self, key: str, resp: ProgressiveResponse) -> None:
        meta = json.dumps(
            {
                "total_lines": resp.total_lines,
                "content_type": resp.content_type,
                "structure_hint": resp.structure_hint,
                "access_count": resp.access_count,
            },
            ensure_ascii=False,
        )
        selection = PendingSelection(
            chunks={"__content__": resp.content, "__meta__": meta},
            format="progressive",
            created_at=resp.created_at,
            total_chars=resp.total_chars,
        )
        self._store.put(key, selection)

    def get(self, key: str) -> ProgressiveResponse | None:
        sel = self._store.get(key)
        if sel is None or sel.format != "progressive":
            return None
        meta = json.loads(sel.chunks.get("__meta__", "{}"))
        return ProgressiveResponse(
            content=sel.chunks["__content__"],
            total_chars=sel.total_chars,
            total_lines=meta.get("total_lines", 0),
            content_type=meta.get("content_type", "text"),
            structure_hint=meta.get("structure_hint", ""),
            created_at=sel.created_at,
            access_count=meta.get("access_count", 0),
        )

    def touch(self, key: str) -> None:
        self._store.touch(key)

    def delete(self, key: str) -> None:
        self._store.delete(key)

    def evict(self, ttl: float, max_size: int) -> None:
        self._store.evict_expired(ttl)
        self._store.evict_oldest(max_size)


class ProgressiveChunker:
    """Splits content into cursor-based chunks with metadata footers."""

    def __init__(self, chunk_size: int = 4000, include_hint: bool = True) -> None:
        self._chunk_size = chunk_size
        self._include_hint = include_hint

    def first_chunk(self, content: str, key: str) -> str:
        """Return the first chunk of *content* with a progressive metadata footer."""
        end = self._find_boundary(content, self._chunk_size)
        chunk = content[:end]
        remaining = len(content) - end
        has_more = remaining > 0

        footer = self._build_footer(
            key=key,
            start=0,
            end=end,
            total=len(content),
            remaining=remaining,
            has_more=has_more,
            content=content,
            next_offset=end,
        )
        return chunk + footer

    def read_chunk(self, content: str, offset: int, limit: int | None = None) -> str:
        """Return a chunk starting at *offset* with a progressive metadata footer."""
        if offset >= len(content):
            return "(no more content)"

        chunk_size = limit or self._chunk_size
        target_end = min(offset + chunk_size, len(content))

        if target_end >= len(content):
            # Last chunk — return everything remaining
            chunk = content[offset:]
            footer = self._build_footer(
                key="",
                start=offset,
                end=len(content),
                total=len(content),
                remaining=0,
                has_more=False,
                content=content,
                next_offset=len(content),
            )
            return chunk + footer

        end = self._find_boundary(content, target_end, floor_offset=offset)
        chunk = content[offset:end]
        remaining = len(content) - end
        footer = self._build_footer(
            key="",
            start=offset,
            end=end,
            total=len(content),
            remaining=remaining,
            has_more=True,
            content=content,
            next_offset=end,
        )
        return chunk + footer

    def _build_footer(
        self,
        *,
        key: str,
        start: int,
        end: int,
        total: int,
        remaining: int,
        has_more: bool,
        content: str,
        next_offset: int,
    ) -> str:
        parts = [f"\n---\n[progressive: chars={start}-{end}/{total}"]
        parts.append(f" | remaining={remaining} | has_more={has_more}]")

        if has_more and self._include_hint:
            hint = self._remaining_headings(content, end)
            if hint:
                parts.append(f"\n[Remaining: {hint}]")

        if has_more and key:
            parts.append(f'\n[-> stm_proxy_read_more(key="{key}", offset={next_offset})]')
        elif has_more:
            parts.append(f"\n[-> stm_proxy_read_more(offset={next_offset})]")

        return "".join(parts)

    @staticmethod
    def _find_boundary(text: str, target: int, floor_offset: int = 0) -> int:
        """Find a natural break point at or before *target*.

        Priority: paragraph (\\n\\n) > line (\\n) > word (space) > hard cut.
        Searches backward from *target*, not going below 80% of the chunk span.
        """
        if target >= len(text):
            return len(text)

        span = target - floor_offset
        floor = max(floor_offset, target - int(span * 0.2))

        # Paragraph boundary
        for i in range(target, floor - 1, -1):
            if i + 1 < len(text) and text[i : i + 2] == "\n\n":
                return i
        # Line boundary
        for i in range(target, floor - 1, -1):
            if text[i] == "\n":
                return i + 1
        # Word boundary
        for i in range(target, floor - 1, -1):
            if text[i] == " ":
                return i + 1
        return target

    @staticmethod
    def _remaining_headings(content: str, from_offset: int) -> str:
        """Return a compact list of markdown headings after *from_offset*."""
        remaining = content[from_offset:]
        headings = _HEADING_RE.findall(remaining)
        if not headings:
            return ""
        # Show up to 5 headings
        shown = headings[:5]
        titles = ", ".join(f'"{h.strip()}"' for h in shown)
        if len(headings) > 5:
            titles += f", ... (+{len(headings) - 5} more)"
        return titles

    @staticmethod
    def detect_content_type(text: str) -> str:
        """Detect content type for metadata."""
        stripped = text.strip()
        if stripped and stripped[0] in "{[":
            try:
                json.loads(stripped)
                return "json"
            except (json.JSONDecodeError, ValueError):
                pass
        if re.search(r"(?:^|\n)#{1,6}\s", text):
            return "markdown"
        if re.search(r"(?:^|\n)\s*(?:def |class |async def |function |func |import |from )", text):
            return "code"
        return "text"

    @staticmethod
    def structure_hint(text: str) -> str:
        """Summarize structural elements in the content."""
        counts: list[str] = []
        headings = len(re.findall(r"(?:^|\n)#{1,6}\s", text))
        code_blocks = text.count("```") // 2
        lines = text.count("\n") + 1
        if headings:
            counts.append(f"{headings} headings")
        if code_blocks:
            counts.append(f"{code_blocks} code blocks")
        counts.append(f"{lines} lines")
        return ", ".join(counts)
