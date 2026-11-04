"""Pin ``append_entry`` output to the canonical blockquote tag format.

A regression that reverts to the legacy lazy-continuation form
(``tags: [...]`` without ``> `` prefix) or to Python ``repr()``
(single-quoted) breaks the chunker contract documented in
``planning/mem-add-tags-blockquote-promote-rfc.md``. This test makes
either drift fail loudly.
"""

import json
import re
from pathlib import Path

import pytest

from memtomem.chunking.markdown import MarkdownChunker
from memtomem.tools.memory_writer import append_entry, format_entry_block


class TestAppendEntryTagFormat:
    def test_tag_line_uses_explicit_blockquote_prefix(self, tmp_path: Path):
        """``> tags:`` line carries an explicit ``> `` prefix."""
        f = tmp_path / "out.md"
        append_entry(f, "Body text.", title="Heading", tags=["a", "b"])
        text = f.read_text(encoding="utf-8")
        # Must contain the canonical line verbatim.
        assert '> tags: ["a", "b"]' in text
        # Legacy lazy-continuation form must NOT appear.
        assert "\ntags: [" not in text
        assert "\ntags: '" not in text

    def test_tags_serialized_as_json(self, tmp_path: Path):
        """JSON (double-quoted) form, not Python ``repr()`` (single-quoted)."""
        f = tmp_path / "out.md"
        append_entry(f, "Body.", title="H", tags=["cache", "shared-from=abc"])
        text = f.read_text(encoding="utf-8")
        assert '> tags: ["cache", "shared-from=abc"]' in text
        # Python repr would produce single quotes; reject.
        assert "'cache'" not in text

    def test_tags_round_trip_via_json(self, tmp_path: Path):
        """The emitted tag list parses cleanly as JSON."""
        f = tmp_path / "out.md"
        tags_in = ["alpha", "beta", "shared-from=xyz", "with space"]
        append_entry(f, "Body.", title="H", tags=tags_in)
        text = f.read_text(encoding="utf-8")
        # Locate the tag line and parse the bracketed value.
        line = next(line for line in text.splitlines() if line.startswith("> tags: "))
        value = line.split("> tags: ", 1)[1]
        assert json.loads(value) == tags_in

    def test_no_tag_line_when_tags_empty(self, tmp_path: Path):
        """Empty / None tags omit the tag line entirely (today's behavior)."""
        f = tmp_path / "out.md"
        append_entry(f, "Body.", title="H", tags=None)
        assert "tags:" not in f.read_text(encoding="utf-8")

        f2 = tmp_path / "out2.md"
        append_entry(f2, "Body.", title="H", tags=[])
        assert "tags:" not in f2.read_text(encoding="utf-8")

    def test_created_line_carries_blockquote_prefix(self, tmp_path: Path):
        """``> created:`` keeps its prefix unchanged (no regression there)."""
        f = tmp_path / "out.md"
        append_entry(f, "Body.", title="H", tags=["x"])
        text = f.read_text(encoding="utf-8")
        assert "\n> created: " in text

    def test_non_ascii_tags_written_as_characters_not_escapes(self, tmp_path: Path):
        """Hangul tags land in the file as real characters, not ``\\uXXXX`` text.

        ``ensure_ascii=True`` (the old default) wrote ``["\\ucee4..."]`` — the
        literal escape sequence — which the hand-split parser then stored
        verbatim, surfacing ``\\ucee4...`` in the tag cloud.
        """
        f = tmp_path / "out.md"
        tags_in = ["커리큘럼설계", "재사용자산"]
        append_entry(f, "Body.", title="제목", tags=tags_in)
        text = f.read_text(encoding="utf-8")
        assert '> tags: ["커리큘럼설계", "재사용자산"]' in text
        # No literal backslash-u escape sequence may survive.
        assert "\\u" not in text
        # And the line still parses as JSON back to the originals.
        line = next(line for line in text.splitlines() if line.startswith("> tags: "))
        assert json.loads(line.split("> tags: ", 1)[1]) == tags_in


def _without_time(block: str) -> str:
    return re.sub(r"> created: \S+", "> created: T", block)


class TestContentWithItsOwnHeading:
    """Content opening with its own ``## `` heading keeps it, and the header
    goes directly under it — where the chunker reads an entry's header (#2762)."""

    def test_header_sits_under_the_contents_heading(self):
        block = format_entry_block("## Mine\n\nbody\n\n### Sub\n\nmore", tags=["t"])
        assert _without_time(block) == (
            '\n## Mine\n\n> created: T\n> tags: ["t"]\n\nbody\n\n### Sub\n\nmore\n'
        )

    def test_title_is_unused(self):
        block = format_entry_block("## Mine\n\nbody", title="Ignored", tags=["t"])
        assert "Ignored" not in block
        assert block.startswith("\n## Mine\n\n> created: ")

    def test_no_tags_writes_the_created_line_alone(self):
        assert _without_time(format_entry_block("## Mine\n\nbody")) == (
            "\n## Mine\n\n> created: T\n\nbody\n"
        )

    def test_an_indented_first_body_line_keeps_its_indentation(self):
        """Only blank lines are dropped between heading and body: an indented
        code block stays a code block."""
        block = format_entry_block("## Mine\n\n\n    code line\nafter", tags=["t"])
        assert block.endswith('> tags: ["t"]\n\n    code line\nafter\n')

    def test_a_crlf_heading_line_is_kept_verbatim(self):
        block = format_entry_block("## Mine\r\n\r\nbody\r\n", tags=["t"])
        assert block.startswith("\n## Mine\r\n\n> created: ")
        assert block.endswith('> tags: ["t"]\n\nbody\n')

    def test_heading_only_content(self):
        assert _without_time(format_entry_block("## Mine", tags=["t"])) == (
            '\n## Mine\n\n> created: T\n> tags: ["t"]\n\n\n'
        )

    @pytest.mark.parametrize(
        ("content", "generated"),
        [
            # The chunker does not read ``## `` alone as a heading, so a header
            # under it would lead nothing.
            ("## \n\nbody", True),
            ("## \r\nbody", True),
            # ``##`` and whitespace only is a heading to the chunker.
            ("##   \nbody", False),
            ("##\tTabbed\n\nbody", False),
        ],
    )
    def test_the_chunker_decides_what_is_a_heading(self, content, generated):
        block = format_entry_block(content, tags=["t"])
        assert block.startswith("\n## Entry ") is generated
        chunks = MarkdownChunker().chunk_file(Path("/tmp/notes.md"), block)
        (holder,) = [c for c in chunks if "body" in c.content]
        assert holder.metadata.tags == ("t",)
        assert holder.metadata.entry_created_at is not None
        assert "> created:" not in holder.content

    @pytest.mark.parametrize("content", ["### Deeper\n\nbody", "#NoSpace\n\nbody"])
    def test_other_first_lines_get_a_generated_heading(self, content):
        assert format_entry_block(content, tags=["t"]).startswith("\n## Entry ")
