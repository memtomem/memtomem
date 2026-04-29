"""Frontmatter validity-window parsing, schema, and indexer-level threading.

Covers Goals 1+2+3 of the temporal-validity RFC: the frontmatter parser
(``_parse_validity_bound`` / ``_extract_validity_window``), the
``ChunkMetadata.valid_from_unix`` / ``valid_to_unix`` fields, the schema
migration that adds the two SQLite columns, and the chunker→metadata wiring.
The pipeline filter, ``mem_search(as_of=...)``, and CLI/Web surfaces are
covered by later RFC PRs.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import sqlite_vec

from memtomem.chunking.markdown import MarkdownChunker, _parse_validity_bound
from memtomem.storage.sqlite_meta import MetaManager
from memtomem.storage.sqlite_schema import create_tables

_extract_validity_window_for_test = MarkdownChunker._extract_validity_window


def _ts(year: int, month: int, day: int, hour: int = 0, minute: int = 0, sec: int = 0) -> int:
    return int(datetime(year, month, day, hour, minute, sec, tzinfo=timezone.utc).timestamp())


class TestParseValidityBoundDate:
    def test_date_lower_bound_is_day_start_utc(self) -> None:
        assert _parse_validity_bound("2025-08-15", upper=False) == _ts(2025, 8, 15, 0, 0, 0)

    def test_date_upper_bound_is_day_end_utc(self) -> None:
        assert _parse_validity_bound("2025-08-15", upper=True) == _ts(2025, 8, 15, 23, 59, 59)

    def test_invalid_month_returns_none(self) -> None:
        assert _parse_validity_bound("2025-13-01", upper=False) is None

    def test_invalid_day_returns_none(self) -> None:
        assert _parse_validity_bound("2025-02-30", upper=True) is None

    def test_garbage_returns_none(self) -> None:
        assert _parse_validity_bound("not-a-date", upper=False) is None
        assert _parse_validity_bound("", upper=True) is None


class TestParseValidityBoundQuarter:
    def test_q1_lower_is_jan_1(self) -> None:
        assert _parse_validity_bound("2025-Q1", upper=False) == _ts(2025, 1, 1, 0, 0, 0)

    def test_q1_upper_is_mar_31_end_of_day(self) -> None:
        assert _parse_validity_bound("2025-Q1", upper=True) == _ts(2025, 3, 31, 23, 59, 59)

    def test_q3_lower_is_jul_1(self) -> None:
        assert _parse_validity_bound("2025-Q3", upper=False) == _ts(2025, 7, 1, 0, 0, 0)

    def test_q4_upper_crosses_year_boundary(self) -> None:
        """Q4 ends Dec 31 — check the year-rollover branch in the parser."""
        assert _parse_validity_bound("2025-Q4", upper=True) == _ts(2025, 12, 31, 23, 59, 59)

    def test_invalid_quarter_zero_returns_none(self) -> None:
        assert _parse_validity_bound("2025-Q0", upper=False) is None

    def test_invalid_quarter_five_returns_none(self) -> None:
        assert _parse_validity_bound("2025-Q5", upper=True) is None


class TestExtractValidityWindow:
    def test_no_frontmatter_returns_none_pair(self) -> None:
        vfrom, vto = _extract_validity_window_for_test("# Heading\n\nbody\n")
        assert vfrom is None and vto is None

    def test_frontmatter_without_validity_keys_returns_none_pair(self) -> None:
        content = "---\ntags: [a, b]\n---\n\n# H\n"
        vfrom, vto = _extract_validity_window_for_test(content)
        assert vfrom is None and vto is None

    def test_only_valid_from_present(self) -> None:
        content = "---\nvalid_from: 2025-08-15\n---\n\nbody\n"
        vfrom, vto = _extract_validity_window_for_test(content)
        assert vfrom == _ts(2025, 8, 15, 0, 0, 0)
        assert vto is None

    def test_only_valid_to_present(self) -> None:
        content = "---\nvalid_to: 2026-Q1\n---\n\nbody\n"
        vfrom, vto = _extract_validity_window_for_test(content)
        assert vfrom is None
        assert vto == _ts(2026, 3, 31, 23, 59, 59)

    def test_both_fields_present(self) -> None:
        content = "---\nvalid_from: 2025-08-15\nvalid_to: 2026-Q1\n---\n\nbody\n"
        vfrom, vto = _extract_validity_window_for_test(content)
        assert vfrom == _ts(2025, 8, 15, 0, 0, 0)
        assert vto == _ts(2026, 3, 31, 23, 59, 59)

    def test_quoted_values_parse(self) -> None:
        """YAML allows quoted scalars — strip surrounding quotes before parsing."""
        content = "---\nvalid_from: '2025-08-15'\nvalid_to: \"2026-Q1\"\n---\n\nbody\n"
        vfrom, vto = _extract_validity_window_for_test(content)
        assert vfrom == _ts(2025, 8, 15, 0, 0, 0)
        assert vto == _ts(2026, 3, 31, 23, 59, 59)

    def test_malformed_value_drops_only_that_side(self) -> None:
        """A typo on one side must not poison the other side."""
        content = "---\nvalid_from: 2025-13-01\nvalid_to: 2026-Q1\n---\n\nbody\n"
        vfrom, vto = _extract_validity_window_for_test(content)
        assert vfrom is None
        assert vto == _ts(2026, 3, 31, 23, 59, 59)

    def test_other_frontmatter_keys_coexist(self) -> None:
        content = (
            "---\n"
            "tags: [policy]\n"
            "valid_from: 2025-08-15\n"
            "valid_to: 2026-Q1\n"
            "---\n"
            "\n# Heading\nbody\n"
        )
        vfrom, vto = _extract_validity_window_for_test(content)
        assert vfrom == _ts(2025, 8, 15, 0, 0, 0)
        assert vto == _ts(2026, 3, 31, 23, 59, 59)


class TestChunkerWiring:
    def test_validity_propagates_to_every_chunk(self) -> None:
        """File-level validity attaches to every chunk produced from the file."""
        content = (
            "---\n"
            "valid_from: 2025-08-15\n"
            "valid_to: 2026-Q1\n"
            "---\n"
            "\n"
            "# Section A\n"
            "alpha body.\n"
            "\n"
            "# Section B\n"
            "beta body.\n"
        )
        chunks = MarkdownChunker().chunk_file(Path("/test.md"), content)
        assert chunks, "chunker must produce at least one chunk"
        for c in chunks:
            assert c.metadata.valid_from_unix == _ts(2025, 8, 15, 0, 0, 0)
            assert c.metadata.valid_to_unix == _ts(2026, 3, 31, 23, 59, 59)

    def test_no_frontmatter_means_both_none(self) -> None:
        content = "# Heading\n\nbody\n"
        chunks = MarkdownChunker().chunk_file(Path("/test.md"), content)
        assert chunks
        for c in chunks:
            assert c.metadata.valid_from_unix is None
            assert c.metadata.valid_to_unix is None

    def test_only_valid_from_propagates_partial_window(self) -> None:
        content = "---\nvalid_from: 2025-08-15\n---\n\n# H\n\nbody\n"
        chunks = MarkdownChunker().chunk_file(Path("/test.md"), content)
        assert chunks
        for c in chunks:
            assert c.metadata.valid_from_unix == _ts(2025, 8, 15, 0, 0, 0)
            assert c.metadata.valid_to_unix is None


def _connect_with_vec() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    return db


def _initialize(db: sqlite3.Connection) -> None:
    meta = MetaManager(lambda: db)
    create_tables(
        db,
        meta,
        dimension=0,
        embedding_provider="none",
        embedding_model="",
    )


class TestSchemaMigration:
    def test_columns_added_with_correct_type_and_nullable(self) -> None:
        db = _connect_with_vec()
        try:
            _initialize(db)
            cols = {row[1]: row for row in db.execute("PRAGMA table_info(chunks)").fetchall()}
            assert "valid_from_unix" in cols
            assert "valid_to_unix" in cols
            # PRAGMA table_info row: (cid, name, type, notnull, dflt_value, pk)
            assert cols["valid_from_unix"][2].upper() == "INTEGER"
            assert cols["valid_to_unix"][2].upper() == "INTEGER"
            assert cols["valid_from_unix"][3] == 0, "must be nullable (notnull=0)"
            assert cols["valid_to_unix"][3] == 0, "must be nullable (notnull=0)"
        finally:
            db.close()

    def test_create_tables_is_idempotent(self) -> None:
        """Re-running ``create_tables`` on the same DB must not error on the
        ``ALTER TABLE ADD COLUMN`` for the new validity columns."""
        db = _connect_with_vec()
        try:
            _initialize(db)
            _initialize(db)  # must not raise
        finally:
            db.close()
