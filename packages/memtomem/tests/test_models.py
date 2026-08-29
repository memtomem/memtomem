"""Tests for models.py — core data models (ChunkType, ChunkMetadata, Chunk,
NamespaceFilter, ContextInfo, SearchResult, IndexingStats)."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from memtomem.models import (
    Chunk,
    ChunkMetadata,
    ChunkType,
    ContextInfo,
    IndexingStats,
    InvalidNamespaceFilterError,
    InvalidScopeFilterError,
    NamespaceFilter,
    ScopeFilter,
    has_namespace_prefix,
    SearchResult,
)


class TestChunkType:
    def test_all_values_are_strings(self):
        for ct in ChunkType:
            assert isinstance(ct.value, str)
            assert ct.value == ct  # StrEnum: value compares equal to member

    def test_markdown_section_equals_literal_string(self):
        assert ChunkType.MARKDOWN_SECTION == "markdown_section"

    def test_unknown_value_raises(self):
        with pytest.raises(ValueError):
            ChunkType("not_a_real_type")


class TestChunkMetadata:
    def test_defaults(self):
        md = ChunkMetadata(source_file=Path("/t.md"))

        assert md.heading_hierarchy == ()
        assert md.chunk_type is ChunkType.RAW_TEXT
        assert md.language == "en"
        assert md.tags == ()
        assert md.namespace == "default"
        assert md.start_line == 0
        assert md.end_line == 0

    def test_is_frozen(self):
        md = ChunkMetadata(source_file=Path("/t.md"))
        with pytest.raises(FrozenInstanceError):
            md.language = "ko"  # type: ignore[misc]


class TestNamespaceFilterParse:
    def test_none_value_without_system_prefixes_returns_none(self):
        assert NamespaceFilter.parse(None) is None

    def test_none_value_with_system_prefixes_returns_exclude_filter(self):
        f = NamespaceFilter.parse(None, system_prefixes=("archive:",))

        assert f is not None
        assert f.exclude_prefixes == ("archive:",)
        assert f.namespaces == ()
        assert f.pattern is None

    def test_single_string_produces_exact_match(self):
        f = NamespaceFilter.parse("work")

        assert f is not None
        assert f.namespaces == ("work",)
        assert f.pattern is None

    def test_comma_separated_produces_union(self):
        f = NamespaceFilter.parse("work, personal ,  misc")

        assert f is not None
        # Values are stripped.
        assert f.namespaces == ("work", "personal", "misc")
        assert f.pattern is None

    def test_glob_pattern_preserved(self):
        f = NamespaceFilter.parse("proj:*")

        assert f is not None
        assert f.pattern == "proj:*"
        assert f.namespaces == ()

    def test_list_input_becomes_namespaces_tuple(self):
        f = NamespaceFilter.parse(["a", "b"])

        assert f is not None
        assert f.namespaces == ("a", "b")
        assert f.pattern is None

    def test_explicit_value_ignores_system_prefixes(self):
        # Caller explicitly named a namespace → opt-in, don't shadow with excludes.
        f = NamespaceFilter.parse("archive:summary", system_prefixes=("archive:",))

        assert f is not None
        assert f.namespaces == ("archive:summary",)
        assert f.exclude_prefixes == ()

    def test_comma_list_mixed_with_a_glob_is_rejected(self):
        """``*`` is checked before ``,``, so this used to parse as one pattern
        containing a literal comma — a LIKE that matches nothing. An empty
        result set is indistinguishable from "no such chunks", so the caller
        never learns the query was malformed."""
        with pytest.raises(InvalidNamespaceFilterError) as excinfo:
            NamespaceFilter.parse("archive:*,work")

        # The message has to name both working spellings; a user who hit this
        # needs to know which half to keep.
        assert "archive:*,work" in str(excinfo.value)

    def test_a_list_argument_may_hold_a_glob_looking_entry(self):
        """The rejection is about the *string* spelling being ambiguous. A
        list is already unambiguous — every entry is an exact name — so a
        value containing ``*`` stays an exact name rather than raising."""
        f = NamespaceFilter.parse(["lit*eral", "work"])

        assert f is not None
        assert f.namespaces == ("lit*eral", "work")
        assert f.pattern is None


class TestScopeFilterParse:
    def test_comma_list_mixed_with_a_glob_is_rejected(self):
        with pytest.raises(InvalidScopeFilterError):
            ScopeFilter.parse("project_*,user")

    def test_plain_glob_and_plain_comma_list_still_parse(self):
        assert ScopeFilter.parse("project_*").pattern == "project_*"
        assert ScopeFilter.parse("user,project_local").scopes == ("user", "project_local")


class TestChunk:
    def test_content_hash_is_auto_generated(self):
        c = Chunk(content="hello", metadata=ChunkMetadata(source_file=Path("/t.md")))

        assert c.content_hash  # non-empty
        assert len(c.content_hash) == 64  # sha256 hex

    def test_content_hash_is_deterministic_for_same_content(self):
        md = ChunkMetadata(source_file=Path("/t.md"))
        a = Chunk(content="same text", metadata=md)
        b = Chunk(content="same text", metadata=md)

        assert a.content_hash == b.content_hash
        # But IDs must still differ (uuid4 default).
        assert a.id != b.id

    def test_content_hash_differs_for_different_content(self):
        md = ChunkMetadata(source_file=Path("/t.md"))
        a = Chunk(content="one", metadata=md)
        b = Chunk(content="two", metadata=md)

        assert a.content_hash != b.content_hash

    def test_content_hash_is_nfc_normalized(self):
        md = ChunkMetadata(source_file=Path("/t.md"))
        # "é" can be encoded as either NFC (single codepoint U+00E9) or NFD
        # (U+0065 + U+0301). After NFC normalization both must hash identically.
        nfc = Chunk(content="caf\u00e9", metadata=md)
        nfd = Chunk(content="cafe\u0301", metadata=md)

        assert nfc.content_hash == nfd.content_hash

    def test_explicit_content_hash_is_preserved(self):
        md = ChunkMetadata(source_file=Path("/t.md"))
        c = Chunk(content="whatever", metadata=md, content_hash="preset")

        assert c.content_hash == "preset"

    def test_retrieval_content_is_plain_when_no_hierarchy(self):
        c = Chunk(content="body", metadata=ChunkMetadata(source_file=Path("/t.md")))

        assert c.retrieval_content == "body"

    def test_retrieval_content_prefixes_hierarchy(self):
        md = ChunkMetadata(
            source_file=Path("/t.md"),
            heading_hierarchy=("Top", "Sub"),
        )
        c = Chunk(content="body text", metadata=md)

        assert c.retrieval_content == "Top > Sub\n\nbody text"


class TestContextInfo:
    def test_defaults(self):
        ctx = ContextInfo()

        assert ctx.window_before == ()
        assert ctx.window_after == ()
        assert ctx.parent_content is None
        assert ctx.chunk_position == 0
        assert ctx.context_tier_used is None

    def test_is_frozen(self):
        ctx = ContextInfo()
        with pytest.raises(FrozenInstanceError):
            ctx.chunk_position = 5  # type: ignore[misc]


class TestSearchResult:
    def test_construction_and_defaults(self):
        chunk = Chunk(content="c", metadata=ChunkMetadata(source_file=Path("/t.md")))
        sr = SearchResult(chunk=chunk, score=0.8, rank=1, source="bm25")

        assert sr.chunk is chunk
        assert sr.score == 0.8
        assert sr.rank == 1
        assert sr.source == "bm25"
        assert sr.context is None

    def test_is_frozen(self):
        chunk = Chunk(content="c", metadata=ChunkMetadata(source_file=Path("/t.md")))
        sr = SearchResult(chunk=chunk, score=0.5, rank=1, source="dense")
        with pytest.raises(FrozenInstanceError):
            sr.score = 0.9  # type: ignore[misc]


class TestIndexingStats:
    def test_defaults_for_optional_fields(self):
        stats = IndexingStats(
            total_files=1,
            total_chunks=5,
            indexed_chunks=5,
            skipped_chunks=0,
            deleted_chunks=0,
            duration_ms=10.0,
        )

        assert stats.errors == ()
        assert stats.new_chunk_ids == ()

    def test_is_frozen(self):
        stats = IndexingStats(
            total_files=1,
            total_chunks=1,
            indexed_chunks=1,
            skipped_chunks=0,
            deleted_chunks=0,
            duration_ms=1.0,
        )
        with pytest.raises(FrozenInstanceError):
            stats.total_files = 2  # type: ignore[misc]


class TestFilterMatchesSqlParity:
    """``matches()`` must decide exactly what the SQL emitter decides (#2192).

    Context-window neighbours are screened in Python because they are read in
    bulk by source file, never through a filtered query. The two evaluators
    therefore have to agree, including on the SQLite ``LIKE`` quirks the
    emitters inherit: ASCII-only case folding, a user-typed ``%`` staying a
    wildcard, and ``_`` being escaped to a literal.
    """

    NAMESPACE_VALUES = (
        "default",
        "archive:x",
        "ARCHIVE:x",
        "archive_x",
        "archiveYx",
        "agent-runtime:planner",
        "a%b",
        "aZZb",
        "ünïx:a",
        "Ünïx:a",
        "back\\slash",
        "back\\\\slash",
        "trailing\\",
    )

    NAMESPACE_FILTERS = (
        NamespaceFilter(namespaces=("archive:x",)),
        NamespaceFilter(namespaces=("default", "archive:x")),
        NamespaceFilter(pattern="archive:*"),
        NamespaceFilter(pattern="ARCHIVE:*"),
        NamespaceFilter(pattern="archive_*"),
        NamespaceFilter(pattern="a%b"),
        NamespaceFilter(pattern="Ünïx:*"),
        NamespaceFilter(pattern="back\\slash"),
        NamespaceFilter(pattern="back\\\\slash"),
        NamespaceFilter(pattern="*\\"),
        NamespaceFilter(exclude_prefixes=("back\\",)),
        NamespaceFilter(exclude_prefixes=("archive:", "agent-runtime:")),
        NamespaceFilter(exclude_prefixes=("ARCHIVE:",)),
        NamespaceFilter(),
    )

    @staticmethod
    def _sql_admits(column: str, fragment: str, params: list, value: str) -> bool:
        import sqlite3

        db = sqlite3.connect(":memory:")
        db.execute(f"CREATE TABLE t ({column} TEXT)")
        db.execute("INSERT INTO t VALUES (?)", (value,))
        sql = f"SELECT COUNT(*) FROM t WHERE {fragment}" if fragment else "SELECT COUNT(*) FROM t"
        return bool(db.execute(sql, params).fetchone()[0])

    @pytest.mark.parametrize("ns_filter", NAMESPACE_FILTERS, ids=lambda f: repr(f))
    def test_namespace_matches_agrees_with_namespace_sql(self, ns_filter):
        from memtomem.storage.sqlite_helpers import namespace_sql

        fragment, params = namespace_sql(ns_filter)
        for value in self.NAMESPACE_VALUES:
            assert ns_filter.matches(value) == self._sql_admits(
                "namespace", fragment, list(params), value
            ), f"{ns_filter!r} disagrees with SQL on {value!r}"

    @pytest.mark.parametrize(
        "scope_filter",
        (
            ScopeFilter(scopes=("user",)),
            ScopeFilter(scopes=("project_shared", "project_local")),
            ScopeFilter(pattern="project_*"),
            ScopeFilter(pattern="PROJECT_*"),
        ),
        ids=lambda f: repr(f),
    )
    def test_scope_matches_agrees_with_scope_context_sql(self, scope_filter):
        from memtomem.storage.sqlite_scope import scope_context_sql

        # No project context: the emitted fragment is the explicit clause
        # alone, which is the part ``matches()`` is responsible for.
        fragment, params = scope_context_sql(scope_filter, None)
        for value in ("user", "project_shared", "project_local", "projectXshared"):
            assert scope_filter.matches(value) == self._sql_admits(
                "scope", fragment, list(params), value
            ), f"{scope_filter!r} disagrees with SQL on {value!r}"

    def test_literal_underscore_is_not_a_wildcard(self):
        """``fnmatch`` would get this wrong; the SQL escapes ``_``."""
        assert NamespaceFilter(pattern="archive_*").matches("archive_x")
        assert not NamespaceFilter(pattern="archive_*").matches("archiveYx")

    def test_trailing_escape_matches_nothing(self):
        """SQLite has nothing to escape there and matches no row."""
        assert not NamespaceFilter(pattern="*\\").matches("trailing\\")

    def test_case_folding_is_ascii_only(self):
        assert NamespaceFilter(pattern="ARCHIVE:*").matches("archive:x")
        assert not NamespaceFilter(pattern="Ünïx:*").matches("ünïx:a")


class TestHasNamespacePrefix:
    def test_folds_ascii_case(self):
        assert has_namespace_prefix("ARCHIVE:2024", ("archive:",))

    def test_prefix_specials_are_literal(self):
        assert has_namespace_prefix("a%b:x", ("a%b:",))
        assert not has_namespace_prefix("aZb:x", ("a%b:",))

    def test_empty_prefixes_match_nothing(self):
        assert not has_namespace_prefix("archive:x", ())
