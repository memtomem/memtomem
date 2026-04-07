"""Tests for compression strategies."""

from __future__ import annotations

import json

from memtomem_stm.proxy.compression import (
    FieldExtractCompressor,
    HybridCompressor,
    NoopCompressor,
    SelectiveCompressor,
    TruncateCompressor,
)


# ---------------------------------------------------------------------------
# NoopCompressor
# ---------------------------------------------------------------------------


class TestNoopCompressor:
    def test_passthrough(self):
        c = NoopCompressor()
        text = "hello world" * 100
        assert c.compress(text, max_chars=10) == text

    def test_empty(self):
        assert NoopCompressor().compress("", max_chars=100) == ""


# ---------------------------------------------------------------------------
# TruncateCompressor
# ---------------------------------------------------------------------------


class TestTruncateCompressor:
    def test_short_text_passthrough(self):
        c = TruncateCompressor()
        assert c.compress("short", max_chars=100) == "short"

    def test_truncates_at_sentence_boundary(self):
        text = "First sentence. Second sentence. Third sentence."
        result = c = TruncateCompressor().compress(text, max_chars=20)
        assert "First sentence." in result
        assert "truncated" in result

    def test_truncation_metadata_includes_summary(self):
        text = "# Heading\n\nSome text.\n\n```code```\n\n- item1\n- item2"
        text = text * 20  # Make it long
        result = TruncateCompressor().compress(text, max_chars=100)
        assert "truncated" in result
        assert "original:" in result

    def test_empty_text(self):
        assert TruncateCompressor().compress("", max_chars=10) == ""

    def test_find_break_prefers_sentence_end(self):
        idx = TruncateCompressor._find_break("Hello world. More text here.", 15)
        assert idx == 12  # After "Hello world."


# ---------------------------------------------------------------------------
# SelectiveCompressor
# ---------------------------------------------------------------------------


class TestSelectiveCompressor:
    def test_short_text_passthrough(self):
        c = SelectiveCompressor()
        assert c.compress("short", max_chars=100) == "short"

    def test_json_dict_produces_toc(self):
        data = {"key1": "x" * 200, "key2": "y" * 200, "key3": "z" * 200}
        text = json.dumps(data)
        c = SelectiveCompressor()
        result = c.compress(text, max_chars=100)
        toc = json.loads(result)
        assert toc["type"] == "toc"
        assert "selection_key" in toc
        assert len(toc["entries"]) == 3
        assert toc["entries"][0]["key"] == "key1"

    def test_json_array_produces_toc(self):
        data = [{"item": i, "content": "x" * 100} for i in range(5)]
        text = json.dumps(data)
        c = SelectiveCompressor()
        result = c.compress(text, max_chars=100)
        toc = json.loads(result)
        assert toc["type"] == "toc"
        assert len(toc["entries"]) == 5

    def test_markdown_parsed_by_headings(self):
        text = "# Section A\n\nContent A here.\n\n# Section B\n\nContent B here."
        text = text * 10
        c = SelectiveCompressor()
        result = c.compress(text, max_chars=50)
        toc = json.loads(result)
        assert toc["type"] == "toc"
        assert any(e["key"] == "Section A" for e in toc["entries"])

    def test_select_returns_requested_sections(self):
        data = {"alpha": "AAA" * 100, "beta": "BBB" * 100, "gamma": "CCC" * 100}
        text = json.dumps(data)
        c = SelectiveCompressor()
        result = c.compress(text, max_chars=100)
        toc = json.loads(result)
        key = toc["selection_key"]

        selected = c.select(key, ["alpha", "gamma"])
        assert "AAA" in selected
        assert "CCC" in selected
        assert "BBB" not in selected

    def test_select_expired_key(self):
        c = SelectiveCompressor(pending_ttl_seconds=0.0)
        data = {"a": "x" * 200, "b": "y" * 200}
        text = json.dumps(data)
        result = c.compress(text, max_chars=50)
        toc = json.loads(result)
        key = toc["selection_key"]

        import time
        time.sleep(0.01)
        result = c.select(key, ["a"])
        assert "not found or expired" in result

    def test_select_unknown_sections(self):
        data = {"a": "x" * 200, "b": "y" * 200}
        text = json.dumps(data)
        c = SelectiveCompressor()
        result = c.compress(text, max_chars=50)
        toc = json.loads(result)
        key = toc["selection_key"]

        result = c.select(key, ["nonexistent"])
        assert "No matching sections" in result

    def test_eviction_removes_oldest(self):
        c = SelectiveCompressor(max_pending=2)
        for i in range(3):
            data = {f"k{i}": "x" * 200}
            c.compress(json.dumps(data), max_chars=50)
        assert len(c._store) <= 2

    def test_plain_text_parsed_by_paragraphs(self):
        text = "\n\n".join([f"Paragraph {i} content here." * 10 for i in range(5)])
        c = SelectiveCompressor()
        result = c.compress(text, max_chars=50)
        toc = json.loads(result)
        assert toc["type"] == "toc"


# ---------------------------------------------------------------------------
# FieldExtractCompressor
# ---------------------------------------------------------------------------


class TestFieldExtractCompressor:
    def test_json_dict_truncates_long_strings(self):
        data = {"short": "hi", "long": "word " * 100}
        text = json.dumps(data)
        c = FieldExtractCompressor()
        result = c.compress(text, max_chars=100)
        # Result is JSON with truncated long values
        assert "hi" in result
        assert "..." in result

    def test_json_array_shows_first_items(self):
        data = [{"name": f"item{i}", "value": "x" * 50} for i in range(10)]
        text = json.dumps(data)
        c = FieldExtractCompressor()
        result = c.compress(text, max_chars=100)
        # Shows preview of first items
        assert "item0" in result

    def test_plain_text_head_tail(self):
        lines = [f"Line {i}" for i in range(100)]
        text = "\n".join(lines)
        c = FieldExtractCompressor()
        result = c.compress(text, max_chars=500)
        assert "lines omitted" in result

    def test_short_text_passthrough(self):
        c = FieldExtractCompressor()
        assert c.compress("short", max_chars=100) == "short"


# ---------------------------------------------------------------------------
# HybridCompressor
# ---------------------------------------------------------------------------


class TestHybridCompressor:
    def test_short_text_passthrough(self):
        c = HybridCompressor(head_chars=100)
        assert c.compress("short", max_chars=500) == "short"

    def test_preserves_head_content(self):
        head = "HEAD CONTENT. " * 50
        tail = "# Section\n\nTail content. " * 50
        text = head + tail
        c = HybridCompressor(head_chars=200)
        result = c.compress(text, max_chars=500)
        assert result.startswith("HEAD CONTENT")

    def test_tail_contains_toc_or_truncation(self):
        text = "# Intro\n\nFirst part.\n\n" + "# Detail\n\nDetail content. " * 100
        c = HybridCompressor(head_chars=100)
        result = c.compress(text, max_chars=300)
        assert "Remaining content" in result or "truncated" in result

    def test_falls_back_to_truncate_when_budget_tight(self):
        # head_chars > text length triggers the `len(text) <= self._head_chars` passthrough
        # So use text longer than head_chars but with budget too tight for head+tail
        text = "A long sentence here. " * 500  # ~11000 chars
        c = HybridCompressor(head_chars=5000, min_head_chars=5000)
        result = c.compress(text, max_chars=100)
        assert "truncated" in result
