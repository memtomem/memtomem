"""``replace_chunk_body`` preserves heading + per-entry blockquote header.

The chunker strips the section-leading ``> created: ...`` / ``> tags:
[...]`` blockquote from chunk content (see
``test_chunking_blockquote_tags.py``). ``mem_edit`` therefore typically
receives body-only ``new_content`` and must not erase the metadata
header. ``replace_chunk_body`` is the helper that handles this.
"""

from pathlib import Path

from memtomem.tools.memory_writer import replace_chunk_body


class TestReplaceChunkBody:
    def test_preserves_canonical_blockquote_header(self, tmp_path: Path):
        """Canonical ``> tags: [...]`` header survives a body-only edit."""
        f = tmp_path / "memory.md"
        f.write_text(
            "## Cache\n"
            "\n"
            "> created: 2026-04-24T22:00:00+00:00\n"
            '> tags: ["cache", "decision"]\n'
            "\n"
            "Old body.\n",
            encoding="utf-8",
        )
        # Chunk range: heading line through final body line.
        replace_chunk_body(f, start_line=1, end_line=6, new_content="New body.")
        result = f.read_text(encoding="utf-8")
        assert "## Cache" in result
        assert "> created: 2026-04-24T22:00:00+00:00" in result
        assert '> tags: ["cache", "decision"]' in result
        assert "New body." in result
        assert "Old body." not in result

    def test_preserves_legacy_lazy_continuation_header(self, tmp_path: Path):
        """Pre-RFC ``tags: [...]`` (no ``> `` prefix) is still preserved."""
        f = tmp_path / "memory.md"
        f.write_text(
            "## Note\n\n> created: 2026-04-01T10:00:00+00:00\ntags: ['legacy']\n\nOld body line.\n",
            encoding="utf-8",
        )
        replace_chunk_body(f, start_line=1, end_line=6, new_content="Replaced body.")
        result = f.read_text(encoding="utf-8")
        assert "> created: 2026-04-01T10:00:00+00:00" in result
        assert "tags: ['legacy']" in result
        assert "Replaced body." in result
        assert "Old body line." not in result

    def test_no_blockquote_header_keeps_heading_only(self, tmp_path: Path):
        """User-authored markdown without ``> created:`` keeps the heading."""
        f = tmp_path / "memory.md"
        f.write_text(
            "## Section\n\nOld body content.\n",
            encoding="utf-8",
        )
        replace_chunk_body(f, start_line=1, end_line=3, new_content="New body content.")
        result = f.read_text(encoding="utf-8")
        assert "## Section" in result
        assert "New body content." in result
        assert "Old body content." not in result

    def test_explicit_heading_in_new_content_full_replace(self, tmp_path: Path):
        """``new_content`` starting with ``## `` overrides the original heading."""
        f = tmp_path / "memory.md"
        f.write_text(
            '## Old\n\n> created: 2026-04-01T10:00:00+00:00\n> tags: ["x"]\n\nOld body.\n',
            encoding="utf-8",
        )
        # User explicitly supplies a new heading — full replacement, header
        # preservation does NOT apply.
        replace_chunk_body(
            f,
            start_line=1,
            end_line=6,
            new_content="## New\n\nFresh body without metadata.",
        )
        result = f.read_text(encoding="utf-8")
        assert "## New" in result
        assert "## Old" not in result
        assert "Fresh body without metadata." in result
        # Metadata header is gone — that's the user's explicit choice here.
        assert "> created:" not in result
        assert "> tags:" not in result

    def test_mid_section_subchunk_no_header_to_preserve(self, tmp_path: Path):
        """An oversized section's non-first sub-chunk has no header in its range."""
        f = tmp_path / "memory.md"
        f.write_text(
            "## Big section\n"
            "\n"
            "> created: 2026-04-01T10:00:00+00:00\n"
            "\n"
            "First half of body.\n"
            "\n"
            "Second half of body.\n",
            encoding="utf-8",
        )
        # Edit the second sub-chunk only (lines 7..7) — no header at line 7.
        replace_chunk_body(f, start_line=7, end_line=7, new_content="Replaced second half.")
        result = f.read_text(encoding="utf-8")
        # Header preserved (it was outside the edit range).
        assert "## Big section" in result
        assert "> created: 2026-04-01T10:00:00+00:00" in result
        assert "First half of body." in result
        assert "Replaced second half." in result
        assert "Second half of body." not in result

    def test_trailing_newline_preserved(self, tmp_path: Path):
        """Files with a trailing newline retain it; files without don't gain one."""
        f1 = tmp_path / "with_nl.md"
        f1.write_text("## H\n\nBody.\n", encoding="utf-8")
        replace_chunk_body(f1, 1, 3, "New.")
        assert f1.read_text(encoding="utf-8").endswith("\n")

        f2 = tmp_path / "no_nl.md"
        f2.write_text("## H\n\nBody.", encoding="utf-8")
        replace_chunk_body(f2, 1, 3, "New.")
        assert not f2.read_text(encoding="utf-8").endswith("\n")
