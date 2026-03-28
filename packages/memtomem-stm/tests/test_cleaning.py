"""Tests for DefaultContentCleaner — noise removal before compression."""

from __future__ import annotations

from memtomem_stm.proxy.cleaning import DefaultContentCleaner


def _clean(text: str) -> str:
    return DefaultContentCleaner().clean(text)


class TestCleaningEmpty:
    def test_empty_string(self):
        assert _clean("") == ""

    def test_whitespace_only(self):
        assert _clean("   \n\n  ") == ""


class TestHTMLStripping:
    def test_html_tags_removed(self):
        assert _clean("Hello <b>world</b>!") == "Hello world!"

    def test_self_closing_tags_removed(self):
        assert _clean("Line 1<br/>Line 2") == "Line 1Line 2"

    def test_code_fences_preserved(self):
        text = "Before\n```\n<div>code</div>\n```\nAfter"
        result = _clean(text)
        assert "<div>code</div>" in result

    def test_inline_code_preserved(self):
        text = "Use `<tag>` in your code"
        result = _clean(text)
        assert "`<tag>`" in result

    def test_generic_types_preserved(self):
        text = "Returns List<String> from the API"
        result = _clean(text)
        assert "List<String>" in result


class TestDeduplication:
    def test_duplicate_paragraphs_removed(self):
        text = "First paragraph.\n\nSecond paragraph.\n\nFirst paragraph."
        result = _clean(text)
        assert result.count("First paragraph.") == 1
        assert "Second paragraph." in result

    def test_unique_paragraphs_preserved(self):
        text = "Para A.\n\nPara B.\n\nPara C."
        assert _clean(text) == text


class TestLinkFloodCollapse:
    def test_link_flood_collapsed(self):
        links = "\n".join([f"- [Link {i}](https://example.com/{i})" for i in range(12)])
        result = _clean(links)
        assert "links omitted" in result

    def test_few_links_preserved(self):
        links = "\n".join([f"- [Link {i}](https://example.com/{i})" for i in range(3)])
        result = _clean(links)
        assert "Link 0" in result
        assert "links omitted" not in result


class TestWhitespaceNormalization:
    def test_triple_newlines_collapsed(self):
        text = "Line 1\n\n\n\nLine 2"
        result = _clean(text)
        assert "\n\n\n" not in result
        assert "Line 1" in result and "Line 2" in result


class TestFullPipeline:
    def test_combined_cleaning(self):
        text = (
            "<div>Hello</div>\n\n"
            "Content paragraph.\n\n"
            "Content paragraph.\n\n\n\n"  # duplicate + extra newlines
            "```\n<code/>\n```"
        )
        result = _clean(text)
        assert "<div>" not in result
        assert result.count("Content paragraph.") == 1
        assert "<code/>" in result
