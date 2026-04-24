"""Section-leading blockquote ``tags:`` promotion (mem_add per-entry tags).

``mem_add`` writes per-entry metadata as a leading blockquote header
(``> created: ...`` plus an optional ``tags:`` line). The chunker must
promote that ``tags:`` value into ``ChunkMetadata.tags`` so
``mem_search(tag_filter=...)`` matches, and strip the header from chunk
content so it does not leak into BM25 / embedding inputs. See
``planning/mem-add-tags-blockquote-promote-rfc.md``.
"""

from pathlib import Path

from memtomem.chunking.markdown import MarkdownChunker


def _chunk(content: str):
    return MarkdownChunker().chunk_file(Path("/test.md"), content)


class TestSectionBlockquoteTags:
    def test_canonical_explicit_blockquote_form(self):
        """Post-RFC writer: every line carries ``> ``."""
        content = (
            "## Cache strategy\n"
            "\n"
            "> created: 2026-04-24T22:12:30+00:00\n"
            '> tags: ["cache", "shared-from=abc"]\n'
            "\n"
            "Use redis with a 5-minute TTL.\n"
        )
        chunks = _chunk(content)
        assert len(chunks) == 1
        assert set(chunks[0].metadata.tags) == {"cache", "shared-from=abc"}
        # Header must be stripped from chunk content.
        assert "tags:" not in chunks[0].content
        assert "created:" not in chunks[0].content
        assert "Use redis" in chunks[0].content

    def test_legacy_lazy_continuation_form(self):
        """Pre-RFC writer: ``tags:`` line has no ``> `` prefix; lazy continuation."""
        content = (
            "## Cache strategy\n"
            "\n"
            "> created: 2026-04-24T22:12:30+00:00\n"
            "tags: ['cache', 'shared-from=abc']\n"
            "\n"
            "Use redis with a 5-minute TTL.\n"
        )
        chunks = _chunk(content)
        assert len(chunks) == 1
        assert set(chunks[0].metadata.tags) == {"cache", "shared-from=abc"}
        assert "tags:" not in chunks[0].content
        assert "Use redis" in chunks[0].content

    def test_frontmatter_and_blockquote_compose_via_union(self):
        """File-level frontmatter tags merge with per-section blockquote tags."""
        content = (
            "---\n"
            "tags: [project, api]\n"
            "---\n"
            "\n"
            "## Cache\n"
            "\n"
            '> tags: ["cache"]\n'
            "\n"
            "Body text.\n"
            "\n"
            "## Auth\n"
            "\n"
            "Different section, frontmatter only.\n"
        )
        chunks = _chunk(content)
        # Locate by heading: chunker emits a no-heading frontmatter chunk
        # too, which the indexing engine later merges. We only care about
        # the per-section chunks here.
        cache_chunk = next(
            c
            for c in chunks
            if c.metadata.heading_hierarchy and "Cache" in c.metadata.heading_hierarchy[0]
        )
        auth_chunk = next(
            c
            for c in chunks
            if c.metadata.heading_hierarchy and "Auth" in c.metadata.heading_hierarchy[0]
        )
        # Cache section gets union: frontmatter + blockquote.
        assert set(cache_chunk.metadata.tags) == {"project", "api", "cache"}
        # Auth section keeps frontmatter only — section-level tags do not bleed.
        assert set(auth_chunk.metadata.tags) == {"project", "api"}

    def test_mid_section_blockquote_is_not_promoted(self):
        """A blockquote that appears after body text is left alone."""
        content = (
            "## Notes\n"
            "\n"
            "Some body prose first.\n"
            "\n"
            "> tags: ['leaked', 'must-not-promote']\n"
            "\n"
            "More body after the quoted block.\n"
        )
        chunks = _chunk(content)
        assert len(chunks) == 1
        # No promotion: section is not blockquote-led.
        assert chunks[0].metadata.tags == ()
        # Mid-section blockquote stays in content.
        assert "tags:" in chunks[0].content
        assert "leaked" in chunks[0].content

    def test_section_leading_blockquote_without_tags_is_noop(self):
        """``> created:`` only — no ``tags:`` line — leaves chunk untouched."""
        content = "## Heading\n\n> created: 2026-04-24T22:12:30+00:00\n\nBody text.\n"
        chunks = _chunk(content)
        assert len(chunks) == 1
        assert chunks[0].metadata.tags == ()
        # Without a tags hit we leave the blockquote in content as-is —
        # callers may still want to render the ``created:`` line.
        assert "created:" in chunks[0].content
        assert "Body text" in chunks[0].content

    def test_legacy_repr_form_strip_handles_both_quote_styles(self):
        """Legacy ``repr(list)`` output uses single quotes; canonical uses double."""
        content_single = "## A\n\n> tags: ['x', 'y']\n\nbody\n"
        content_double = '## A\n\n> tags: ["x", "y"]\n\nbody\n'
        chunks_single = _chunk(content_single)
        chunks_double = _chunk(content_double)
        assert set(chunks_single[0].metadata.tags) == {"x", "y"}
        assert set(chunks_double[0].metadata.tags) == {"x", "y"}
