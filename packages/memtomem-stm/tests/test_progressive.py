"""Tests for progressive (cursor-based) delivery."""

from __future__ import annotations

import time

import pytest

from memtomem_stm.proxy.compression import PendingSelection
from memtomem_stm.proxy.config import (
    CompressionStrategy,
    ProgressiveConfig,
)
from memtomem_stm.proxy.pending_store import InMemoryPendingStore
from memtomem_stm.proxy.progressive import (
    ProgressiveChunker,
    ProgressiveResponse,
    ProgressiveStoreAdapter,
)


# ---------------------------------------------------------------------------
# ProgressiveChunker — boundary detection
# ---------------------------------------------------------------------------


class TestProgressiveChunkerBoundary:
    def test_short_content_passthrough(self):
        chunker = ProgressiveChunker(chunk_size=4000)
        text = "short content"
        result = chunker.first_chunk(text, "key1")
        # Content should be fully included + footer with has_more=False
        assert "short content" in result
        assert "has_more=False" in result

    def test_boundary_prefers_line(self):
        """Should cut at line boundary, not mid-word."""
        lines = ["Line " + str(i) + " " + "x" * 50 for i in range(100)]
        text = "\n".join(lines)
        chunker = ProgressiveChunker(chunk_size=200)
        result = chunker.first_chunk(text, "key1")
        # The content portion (before footer) should end at a line boundary
        content_part = result.split("\n---\n")[0]
        assert content_part.endswith("\n") or content_part == text[:200]

    def test_boundary_prefers_paragraph(self):
        """Should prefer paragraph boundary (\\n\\n) when available."""
        text = "First paragraph content.\n\nSecond paragraph content.\n\nThird paragraph."
        chunker = ProgressiveChunker(chunk_size=30)
        result = chunker.first_chunk(text, "key1")
        content_part = result.split("\n---\n")[0]
        # Should cut at the paragraph boundary
        assert content_part.strip().endswith("content.")

    def test_hard_cut_for_long_single_line(self):
        """Falls back to hard cut when no natural boundary exists."""
        text = "a" * 10000  # No spaces, no newlines
        chunker = ProgressiveChunker(chunk_size=4000)
        result = chunker.first_chunk(text, "key1")
        # Should still produce a result without error
        assert "has_more=True" in result


# ---------------------------------------------------------------------------
# ProgressiveChunker — first_chunk metadata
# ---------------------------------------------------------------------------


class TestProgressiveChunkerFirstChunk:
    def test_first_chunk_metadata(self):
        text = "x" * 10000
        chunker = ProgressiveChunker(chunk_size=4000)
        result = chunker.first_chunk(text, "mykey")
        assert "has_more=True" in result
        assert "mykey" in result
        assert "stm_proxy_read_more" in result
        assert "/10000" in result

    def test_first_chunk_includes_remaining_headings(self):
        sections = [f"## Section {i}\n\nContent for section {i}.\n" for i in range(10)]
        text = "\n".join(sections)
        chunker = ProgressiveChunker(chunk_size=100, include_hint=True)
        result = chunker.first_chunk(text, "key1")
        # Should include heading hints for remaining content
        assert "Remaining:" in result or "Section" in result

    def test_no_hint_when_disabled(self):
        sections = [f"## Section {i}\n\nContent for section {i}.\n" for i in range(10)]
        text = "\n".join(sections)
        chunker = ProgressiveChunker(chunk_size=100, include_hint=False)
        result = chunker.first_chunk(text, "key1")
        assert "Remaining:" not in result


# ---------------------------------------------------------------------------
# ProgressiveChunker — read_chunk
# ---------------------------------------------------------------------------


class TestProgressiveChunkerReadChunk:
    def test_read_chunk_at_offset(self):
        text = "A" * 4000 + "\n" + "B" * 4000 + "\n" + "C" * 2000
        chunker = ProgressiveChunker(chunk_size=4000)
        result = chunker.read_chunk(text, offset=4001)
        assert "B" in result
        assert "has_more=" in result

    def test_read_chunk_past_end(self):
        text = "short"
        chunker = ProgressiveChunker(chunk_size=4000)
        result = chunker.read_chunk(text, offset=1000)
        assert "no more content" in result

    def test_last_chunk_has_more_false(self):
        text = "x" * 100
        chunker = ProgressiveChunker(chunk_size=4000)
        result = chunker.read_chunk(text, offset=0)
        assert "has_more=False" in result

    def test_custom_limit(self):
        text = "x" * 10000
        chunker = ProgressiveChunker(chunk_size=4000)
        result = chunker.read_chunk(text, offset=0, limit=500)
        # Content portion should be approximately 500 chars
        content_part = result.split("\n---\n")[0]
        assert len(content_part) <= 600  # boundary detection may add a bit


# ---------------------------------------------------------------------------
# ProgressiveChunker — content integrity
# ---------------------------------------------------------------------------


class TestProgressiveContentIntegrity:
    def test_sequential_read_covers_all_content(self):
        """Reading first + all continuations must reproduce the full original text."""
        text = "Line {i}: " + "x" * 80 + "\n"
        text = "".join(f"Line {i}: {'x' * 80}\n" for i in range(200))
        chunker = ProgressiveChunker(chunk_size=500)

        # Collect all content portions (before footer)
        parts: list[str] = []
        offset = 0

        # First chunk
        result = chunker.first_chunk(text, "key1")
        content = result.split("\n---\n")[0]
        parts.append(content)
        offset = len(content)

        # Read remaining chunks
        for _ in range(100):  # safety limit
            result = chunker.read_chunk(text, offset)
            if "no more content" in result:
                break
            content = result.split("\n---\n")[0]
            parts.append(content)
            offset += len(content)
            if "has_more=False" in result:
                break

        reassembled = "".join(parts)
        assert reassembled == text

    def test_single_chunk_integrity(self):
        """Short content: first_chunk returns it all with has_more=False."""
        text = "Hello, world!"
        chunker = ProgressiveChunker(chunk_size=4000)
        result = chunker.first_chunk(text, "key1")
        assert "Hello, world!" in result
        assert "has_more=False" in result


# ---------------------------------------------------------------------------
# ProgressiveStoreAdapter
# ---------------------------------------------------------------------------


class TestProgressiveStoreAdapter:
    def test_put_get_roundtrip(self):
        store = ProgressiveStoreAdapter(InMemoryPendingStore())
        resp = ProgressiveResponse(
            content="hello world",
            total_chars=11,
            total_lines=1,
            content_type="text",
            structure_hint="1 lines",
            created_at=time.monotonic(),
        )
        store.put("key1", resp)
        got = store.get("key1")
        assert got is not None
        assert got.content == "hello world"
        assert got.total_chars == 11
        assert got.content_type == "text"

    def test_missing_key_returns_none(self):
        store = ProgressiveStoreAdapter(InMemoryPendingStore())
        assert store.get("nonexistent") is None

    def test_touch_does_not_error(self):
        store = ProgressiveStoreAdapter(InMemoryPendingStore())
        resp = ProgressiveResponse(
            content="x",
            total_chars=1,
            total_lines=1,
            content_type="text",
            structure_hint="",
            created_at=time.monotonic(),
        )
        store.put("key1", resp)
        store.touch("key1")  # should not raise

    def test_delete(self):
        store = ProgressiveStoreAdapter(InMemoryPendingStore())
        resp = ProgressiveResponse(
            content="x",
            total_chars=1,
            total_lines=1,
            content_type="text",
            structure_hint="",
            created_at=time.monotonic(),
        )
        store.put("key1", resp)
        store.delete("key1")
        assert store.get("key1") is None

    def test_does_not_interfere_with_selective(self):
        """Progressive and selective entries can coexist in the same store."""
        backend = InMemoryPendingStore()
        adapter = ProgressiveStoreAdapter(backend)

        # Store a progressive entry
        adapter.put(
            "prog1",
            ProgressiveResponse(
                content="progressive",
                total_chars=11,
                total_lines=1,
                content_type="text",
                structure_hint="",
                created_at=time.monotonic(),
            ),
        )

        # Store a selective entry directly
        backend.put(
            "sel1",
            PendingSelection(
                chunks={"intro": "Intro content"},
                format="markdown",
                created_at=time.monotonic(),
                total_chars=100,
            ),
        )

        # Progressive retrieval works
        assert adapter.get("prog1") is not None
        assert adapter.get("prog1").content == "progressive"

        # Selective entry is ignored by adapter (format != "progressive")
        assert adapter.get("sel1") is None

        # But still accessible via backend
        assert backend.get("sel1") is not None


# ---------------------------------------------------------------------------
# Content type detection
# ---------------------------------------------------------------------------


class TestContentTypeDetection:
    def test_json(self):
        assert ProgressiveChunker.detect_content_type('{"key": "value"}') == "json"

    def test_json_array(self):
        assert ProgressiveChunker.detect_content_type("[1, 2, 3]") == "json"

    def test_markdown(self):
        assert ProgressiveChunker.detect_content_type("# Title\nContent") == "markdown"

    def test_code(self):
        assert ProgressiveChunker.detect_content_type("def foo():\n    pass") == "code"

    def test_plain_text(self):
        assert ProgressiveChunker.detect_content_type("Just some plain text.") == "text"


# ---------------------------------------------------------------------------
# Structure hint
# ---------------------------------------------------------------------------


class TestStructureHint:
    def test_markdown_headings_counted(self):
        text = "# H1\n## H2\n### H3\nContent"
        hint = ProgressiveChunker.structure_hint(text)
        assert "3 headings" in hint

    def test_code_blocks_counted(self):
        text = "```python\ncode\n```\nmore\n```js\ncode\n```"
        hint = ProgressiveChunker.structure_hint(text)
        assert "2 code blocks" in hint

    def test_line_count(self):
        text = "line1\nline2\nline3"
        hint = ProgressiveChunker.structure_hint(text)
        assert "3 lines" in hint


# ---------------------------------------------------------------------------
# ProgressiveConfig
# ---------------------------------------------------------------------------


class TestProgressiveConfig:
    def test_defaults(self):
        cfg = ProgressiveConfig()
        assert cfg.chunk_size == 4000
        assert cfg.max_stored == 200
        assert cfg.ttl_seconds == 600.0
        assert cfg.include_structure_hint is True

    def test_strategy_enum_includes_progressive(self):
        assert "progressive" in set(CompressionStrategy)


# ---------------------------------------------------------------------------
# Remaining headings hint
# ---------------------------------------------------------------------------


class TestRemainingHeadings:
    def test_shows_up_to_5_headings(self):
        headings = [f"## Heading {i}\n\nContent\n" for i in range(10)]
        text = "\n".join(headings)
        result = ProgressiveChunker._remaining_headings(text, 0)
        assert "Heading 0" in result
        assert "+5 more" in result

    def test_empty_when_no_headings(self):
        assert ProgressiveChunker._remaining_headings("plain text only", 0) == ""
