"""Tests for usability fixes from 2026-04-06 testing session.

Covers: frontmatter tags, wikilink resolution, template placeholders,
FTS5 hyphen quoting, heading-based merge prevention, action aliases,
display path normalization, session title, cleanup_orphans.
"""

import json
from pathlib import Path


from memtomem.chunking.markdown import MarkdownChunker
from memtomem.server.tools.meta import _help, _ALIASES
from memtomem.server.tool_registry import ACTIONS
from memtomem.storage.fts_tokenizer import tokenize_for_fts
from memtomem.templates import render_template


# ── Frontmatter tag parsing ──────────────────────────────────────────────


class TestFrontmatterTags:
    def test_inline_tags(self):
        chunker = MarkdownChunker()
        content = "---\ntitle: Test\ntags: [alpha, beta, gamma]\n---\n\n## Section\n\nBody text."
        chunks = chunker.chunk_file(Path("/test.md"), content)
        assert len(chunks) >= 1
        assert "alpha" in chunks[0].metadata.tags
        assert "beta" in chunks[0].metadata.tags
        assert "gamma" in chunks[0].metadata.tags

    def test_inline_tags_with_quotes(self):
        chunker = MarkdownChunker()
        content = "---\ntags: ['api', \"backend\"]\n---\n\n## H\n\nText."
        chunks = chunker.chunk_file(Path("/test.md"), content)
        assert "api" in chunks[0].metadata.tags
        assert "backend" in chunks[0].metadata.tags

    def test_no_frontmatter(self):
        chunker = MarkdownChunker()
        content = "## Heading\n\nNo frontmatter here."
        chunks = chunker.chunk_file(Path("/test.md"), content)
        assert chunks[0].metadata.tags == ()

    def test_frontmatter_without_tags(self):
        chunker = MarkdownChunker()
        content = "---\ntitle: Test\nstatus: draft\n---\n\n## H\n\nBody."
        chunks = chunker.chunk_file(Path("/test.md"), content)
        assert chunks[0].metadata.tags == ()

    def test_tags_applied_to_all_chunks(self):
        chunker = MarkdownChunker()
        content = "---\ntags: [shared-tag]\n---\n\n## A\n\nFirst.\n\n## B\n\nSecond."
        chunks = chunker.chunk_file(Path("/test.md"), content)
        for c in chunks:
            if c.content.strip():
                assert "shared-tag" in c.metadata.tags


# ── Wikilink resolution ──────────────────────────────────────────────────


class TestWikilinkResolution:
    def test_simple_wikilink(self):
        chunker = MarkdownChunker()
        content = "## Notes\n\nSee [[other-page]] for details."
        chunks = chunker.chunk_file(Path("/test.md"), content)
        assert "[[" not in chunks[0].content
        assert "other-page" in chunks[0].content

    def test_aliased_wikilink(self):
        chunker = MarkdownChunker()
        content = "## Notes\n\nRefer to [[api-redesign|the API project]]."
        chunks = chunker.chunk_file(Path("/test.md"), content)
        assert "[[" not in chunks[0].content
        assert "the API project" in chunks[0].content
        assert "api-redesign" not in chunks[0].content

    def test_multiple_wikilinks(self):
        chunker = MarkdownChunker()
        content = "## Links\n\n[[a]], [[b|B label]], and [[c]]."
        chunks = chunker.chunk_file(Path("/test.md"), content)
        text = chunks[0].content
        assert "a" in text
        assert "B label" in text
        assert "c" in text
        assert "[[" not in text


# ── Template placeholder removal ─────────────────────────────────────────


class TestTemplatePlaceholders:
    def test_missing_fields_removed(self):
        result = render_template("meeting", json.dumps({
            "title": "Standup",
            "attendees": "Team",
            "decisions": "None",
        }))
        assert "(fill:" not in result
        assert "Agenda" not in result  # removed because not provided
        assert "Attendees" in result   # provided field stays

    def test_heading_with_placeholder_title_kept(self):
        result = render_template("debug", "Crash on boot")
        assert "Debug:" in result  # heading kept even if title is placeholder

    def test_all_fields_provided_no_removal(self):
        result = render_template("meeting", json.dumps({
            "title": "Sprint",
            "attendees": "All",
            "agenda": "Review",
            "decisions": "Ship it",
            "action_items": "Deploy",
        }))
        assert "Attendees" in result
        assert "Agenda" in result
        assert "Decisions" in result
        assert "Action Items" in result


# ── FTS5 hyphen quoting ──────────────────────────────────────────────────


class TestFTS5HyphenQuoting:
    def test_hyphenated_word_quoted(self):
        result = tokenize_for_fts("status in-progress project", for_query=True)
        assert '"in-progress"' in result
        assert "status*" in result
        assert "project*" in result

    def test_normal_words_get_wildcard(self):
        result = tokenize_for_fts("hello world", for_query=True)
        assert result == "hello* world*"

    def test_colon_in_word_quoted(self):
        result = tokenize_for_fts("project:myapp", for_query=True)
        assert '"' in result

    def test_empty_string(self):
        assert tokenize_for_fts("", for_query=True) == ""


# ── Heading-based merge prevention ───────────────────────────────────────


class TestHeadingMerge:
    def test_different_headings_not_merged(self):
        """Two short sections with different headings should stay separate."""
        from memtomem.indexing.engine import _merge_short_chunks
        from memtomem.models import Chunk, ChunkMetadata, ChunkType

        c1 = Chunk(
            content="Short entry one.",
            metadata=ChunkMetadata(
                source_file=Path("/test.md"),
                heading_hierarchy=("## Entry A",),
                chunk_type=ChunkType.MARKDOWN_SECTION,
                start_line=1,
                end_line=3,
            ),
        )
        c2 = Chunk(
            content="Short entry two.",
            metadata=ChunkMetadata(
                source_file=Path("/test.md"),
                heading_hierarchy=("## Entry B",),
                chunk_type=ChunkType.MARKDOWN_SECTION,
                start_line=4,
                end_line=6,
            ),
        )
        result = _merge_short_chunks([c1, c2], min_tokens=128, max_tokens=512)
        assert len(result) == 2, "Chunks with different headings should not merge"

    def test_same_heading_can_merge(self):
        from memtomem.indexing.engine import _merge_short_chunks
        from memtomem.models import Chunk, ChunkMetadata, ChunkType

        c1 = Chunk(
            content="Part one.",
            metadata=ChunkMetadata(
                source_file=Path("/test.md"),
                heading_hierarchy=("## Same",),
                chunk_type=ChunkType.MARKDOWN_SECTION,
                start_line=1,
                end_line=2,
            ),
        )
        c2 = Chunk(
            content="Part two.",
            metadata=ChunkMetadata(
                source_file=Path("/test.md"),
                heading_hierarchy=("## Same",),
                chunk_type=ChunkType.MARKDOWN_SECTION,
                start_line=3,
                end_line=4,
            ),
        )
        result = _merge_short_chunks([c1, c2], min_tokens=128, max_tokens=512)
        assert len(result) == 1, "Chunks with same heading should merge when short"


# ── Action aliases ───────────────────────────────────────────────────────


class TestActionAliases:
    def test_aliases_resolve_to_real_actions(self):
        for alias, target in _ALIASES.items():
            assert target in ACTIONS, f"Alias '{alias}' points to non-existent action '{target}'"

    def test_health_report_alias(self):
        assert _ALIASES["health_report"] == "eval"

    def test_namespace_set_alias(self):
        assert _ALIASES["namespace_set"] == "ns_set"

    def test_orphans_alias(self):
        assert _ALIASES["orphans"] == "cleanup_orphans"


# ── Display path ─────────────────────────────────────────────────────────


class TestDisplayPath:
    def test_private_tmp_stripped(self):
        import sys
        from memtomem.server.formatters import _display_path

        if sys.platform == "darwin":
            assert _display_path("/private/tmp/test/file.md") == "/tmp/test/file.md"

    def test_normal_path_unchanged(self):
        from memtomem.server.formatters import _display_path

        assert _display_path("/Users/me/notes/test.md") == "/Users/me/notes/test.md"


# ── Tool registry param_docs ─────────────────────────────────────────────


class TestParamDocs:
    def test_policy_add_has_param_docs(self):
        info = ACTIONS.get("policy_add")
        assert info is not None
        assert "policy_type" in info.param_docs
        assert "auto_archive" in info.param_docs["policy_type"]

    def test_help_shows_param_docs(self):
        result = _help(category="policy")
        assert "auto_archive" in result

    def test_session_start_has_title_doc(self):
        info = ACTIONS.get("session_start")
        assert info is not None
        assert "title" in info.params


# ── ns_assign registered ─────────────────────────────────────────────────


class TestNsAssign:
    def test_ns_assign_registered(self):
        assert "ns_assign" in ACTIONS
        assert ACTIONS["ns_assign"].category == "namespace"

    def test_ns_assign_params(self):
        info = ACTIONS["ns_assign"]
        assert "namespace" in info.params
        assert "source_filter" in info.params


# ── cleanup_orphans registered ───────────────────────────────────────────


class TestCleanupOrphans:
    def test_cleanup_orphans_registered(self):
        assert "cleanup_orphans" in ACTIONS
        assert ACTIONS["cleanup_orphans"].category == "maintenance"

    def test_cleanup_orphans_has_dry_run(self):
        info = ACTIONS["cleanup_orphans"]
        assert "dry_run" in info.params
