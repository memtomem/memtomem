"""Frontmatter validity-window parsing, schema, indexer threading, pipeline filter, MCP + CLI surfaces.

Covers Goals 1+2+3+4+5+6 of the temporal-validity RFC: the frontmatter parser
(``_parse_validity_bound`` / ``_extract_validity_window``), the
``ChunkMetadata.valid_from_unix`` / ``valid_to_unix`` fields, the schema
migration that adds the two SQLite columns, the chunker→metadata wiring,
the ``_apply_validity_filter`` pipeline stage with its
``SearchPipeline.search(as_of_unix=...)`` plumbing, the
``mem_search(as_of=...)`` MCP-tool surface, and the CLI surfaces:
``mm search --as-of <date>`` and the conditional Validity column on
``mm recall`` (the actual chunks-listing CLI; the RFC's "mm list" name
was a drift — corrected in the same release window). The Web UI badge
(Goal 7) is covered by a later RFC PR.

The search round-trip tests in the middle of the file lock in the fix
for PR #533 review feedback — bm25_search / dense_search must carry the
new columns through ``_row_to_chunk`` so the validity_filter stage can
rely on chunks emerging from the search path with their windows intact.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
import sqlite_vec

from memtomem.chunking.markdown import MarkdownChunker, _parse_validity_bound
from memtomem.models import Chunk, ChunkMetadata, SearchResult
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


def _chunk_with_validity(
    *,
    content: str,
    valid_from_unix: int | None,
    valid_to_unix: int | None,
    embedding: list[float] | None = None,
) -> Chunk:
    """Build a Chunk that carries an explicit validity window.

    Used by the search round-trip tests below — ``helpers.make_chunk`` does
    not surface the new fields, and broadening that helper is out of scope
    for this PR.
    """
    return Chunk(
        content=content,
        metadata=ChunkMetadata(
            source_file=Path("/tmp/validity-search.md"),
            valid_from_unix=valid_from_unix,
            valid_to_unix=valid_to_unix,
        ),
        content_hash=f"hash-{uuid.uuid4().hex[:8]}",
        embedding=embedding if embedding is not None else [0.1] * 1024,
    )


class TestSearchRoundTrip:
    """Search results must carry validity columns through ``_row_to_chunk``.

    PR #533 review uncovered that ``bm25_search`` / ``dense_search`` were
    SELECTing only 13 chunk columns and slicing ``row[:13]`` to
    ``_row_to_chunk``, so the ``len(row) >= 21`` guard never tripped and
    every search-derived chunk carried ``valid_from_unix=None`` regardless
    of what was stored. The fix switches both queries to ``SELECT c.*`` so
    the full row reaches the deserializer. These tests lock that in.
    """

    async def test_bm25_search_preserves_validity_window(self, storage) -> None:
        vfrom = _ts(2025, 8, 15, 0, 0, 0)
        vto = _ts(2026, 3, 31, 23, 59, 59)
        chunk = _chunk_with_validity(
            content="quarterly policy bm25 marker",
            valid_from_unix=vfrom,
            valid_to_unix=vto,
        )
        await storage.upsert_chunks([chunk])

        results = await storage.bm25_search("quarterly policy bm25 marker", top_k=5)
        assert results, "BM25 search must return the seeded chunk"
        assert results[0].chunk.metadata.valid_from_unix == vfrom
        assert results[0].chunk.metadata.valid_to_unix == vto

    async def test_dense_search_preserves_validity_window(self, storage) -> None:
        vfrom = _ts(2025, 1, 1, 0, 0, 0)
        vto = _ts(2025, 12, 31, 23, 59, 59)
        emb = [0.2 + i * 0.0001 for i in range(1024)]
        chunk = _chunk_with_validity(
            content="dense-search validity target",
            valid_from_unix=vfrom,
            valid_to_unix=vto,
            embedding=emb,
        )
        await storage.upsert_chunks([chunk])

        results = await storage.dense_search(emb, top_k=5)
        assert results, "Dense search must return the seeded chunk"
        assert results[0].chunk.metadata.valid_from_unix == vfrom
        assert results[0].chunk.metadata.valid_to_unix == vto

    async def test_bm25_search_returns_none_pair_for_unset_chunks(self, storage) -> None:
        """A chunk written without validity stays unbounded through search —
        confirms ``SELECT c.*`` did not silently fabricate values for the
        always-valid (None, None) backward-compat default."""
        chunk = _chunk_with_validity(
            content="no-validity bm25 marker",
            valid_from_unix=None,
            valid_to_unix=None,
        )
        await storage.upsert_chunks([chunk])

        results = await storage.bm25_search("no-validity bm25 marker", top_k=5)
        assert results
        assert results[0].chunk.metadata.valid_from_unix is None
        assert results[0].chunk.metadata.valid_to_unix is None


# ── Goal 4: pipeline-level validity_filter ─────────────────────────────


def _result_with_window(vfrom: int | None, vto: int | None, *, marker: str = "x") -> SearchResult:
    """Build a minimal SearchResult carrying a validity window for filter tests."""
    chunk = Chunk(
        content=f"chunk-{marker}",
        metadata=ChunkMetadata(
            source_file=Path(f"/tmp/{marker}.md"),
            valid_from_unix=vfrom,
            valid_to_unix=vto,
        ),
        id=uuid.uuid4(),
        embedding=[],
    )
    return SearchResult(chunk=chunk, score=1.0, rank=1, source="fused")


class TestApplyValidityFilter:
    """Unit tests for the pure ``_apply_validity_filter`` helper.

    Locks the RFC §Design semantics: inclusive both ends, ``None`` =
    unbounded on that side, ``(None, None)`` = always-valid (opt-in
    default), order preservation.
    """

    def _filter(self, results, as_of_unix):
        from memtomem.search.pipeline import _apply_validity_filter

        return _apply_validity_filter(results, as_of_unix)

    def test_inside_window_passes(self) -> None:
        r = _result_with_window(_ts(2025, 1, 1), _ts(2025, 12, 31, 23, 59, 59))
        assert self._filter([r], _ts(2025, 6, 15)) == [r]

    def test_before_window_excluded(self) -> None:
        r = _result_with_window(_ts(2025, 1, 1), _ts(2025, 12, 31, 23, 59, 59))
        assert self._filter([r], _ts(2024, 12, 31)) == []

    def test_after_window_excluded(self) -> None:
        r = _result_with_window(_ts(2025, 1, 1), _ts(2025, 12, 31, 23, 59, 59))
        assert self._filter([r], _ts(2026, 1, 1)) == []

    def test_boundary_lower_inclusive(self) -> None:
        """``as_of == valid_from`` is inside (RFC §Comparison semantics)."""
        vfrom = _ts(2025, 1, 1)
        r = _result_with_window(vfrom, _ts(2025, 12, 31, 23, 59, 59))
        assert self._filter([r], vfrom) == [r]

    def test_boundary_upper_inclusive(self) -> None:
        """``as_of == valid_to`` is inside (RFC §Comparison semantics)."""
        vto = _ts(2025, 12, 31, 23, 59, 59)
        r = _result_with_window(_ts(2025, 1, 1), vto)
        assert self._filter([r], vto) == [r]

    def test_half_bounded_lower_only(self) -> None:
        """``valid_from`` only — passes for any ``as_of >= valid_from``."""
        vfrom = _ts(2025, 1, 1)
        r = _result_with_window(vfrom, None)
        assert self._filter([r], vfrom) == [r]
        assert self._filter([r], _ts(2099, 1, 1)) == [r]
        assert self._filter([r], _ts(2024, 12, 31)) == []

    def test_half_bounded_upper_only(self) -> None:
        """``valid_to`` only — passes for any ``as_of <= valid_to``."""
        vto = _ts(2025, 12, 31, 23, 59, 59)
        r = _result_with_window(None, vto)
        assert self._filter([r], vto) == [r]
        assert self._filter([r], _ts(1970, 1, 1)) == [r]
        assert self._filter([r], _ts(2026, 1, 1)) == []

    def test_always_valid_passes(self) -> None:
        """``(None, None)`` is the opt-in default — always retained."""
        r = _result_with_window(None, None)
        # Both a "now" and a "long-ago" as_of must keep it.
        assert self._filter([r], _ts(2025, 6, 15)) == [r]
        assert self._filter([r], _ts(1970, 1, 1)) == [r]

    def test_order_preserved(self) -> None:
        """Filter must not reorder survivors — downstream stages depend on rank order."""
        r1 = _result_with_window(_ts(2024, 1, 1), _ts(2026, 1, 1), marker="a")
        r2 = _result_with_window(None, None, marker="b")
        r3 = _result_with_window(_ts(2024, 1, 1), _ts(2026, 1, 1), marker="c")
        out = self._filter([r1, r2, r3], _ts(2025, 6, 15))
        assert [r.chunk.content for r in out] == ["chunk-a", "chunk-b", "chunk-c"]

    def test_excluded_chunks_removed_in_mixed_input(self) -> None:
        """Survivors and rejects mixed in one pass; rejects drop, survivors keep order."""
        keep1 = _result_with_window(_ts(2025, 1, 1), _ts(2025, 12, 31, 23, 59, 59), marker="k1")
        drop = _result_with_window(_ts(2020, 1, 1), _ts(2020, 12, 31, 23, 59, 59), marker="d")
        keep2 = _result_with_window(None, None, marker="k2")
        out = self._filter([keep1, drop, keep2], _ts(2025, 6, 15))
        assert [r.chunk.content for r in out] == ["chunk-k1", "chunk-k2"]


# ── Goal 4: pipeline wiring (as_of_unix plumbing + cache semantics) ────


def _make_validity_pipeline(bm25_results):
    """SearchPipeline wired around AsyncMock storage with controllable BM25 hits.

    Mirrors the fixture pattern in ``tests/test_pipeline.py`` — the
    pipeline is exercised end-to-end so the filter wiring (call site,
    cache gating) is verified against the real ``SearchPipeline.search``
    code path, not a re-implemented stub.
    """
    from unittest.mock import AsyncMock

    from memtomem.config import SearchConfig
    from memtomem.search.pipeline import SearchPipeline

    storage = AsyncMock()
    storage.bm25_search = AsyncMock(return_value=bm25_results)
    storage.dense_search = AsyncMock(return_value=[])
    storage.increment_access = AsyncMock()
    storage.save_query_history = AsyncMock()
    storage.get_access_counts = AsyncMock(return_value={})
    storage.get_embeddings_for_chunks = AsyncMock(return_value={})
    storage.get_importance_scores = AsyncMock(return_value={})
    storage.count_chunks_by_ns_prefix = AsyncMock(return_value=0)

    embedder = AsyncMock()
    embedder.embed_query = AsyncMock(return_value=[0.1] * 8)

    return SearchPipeline(
        storage=storage,
        embedder=embedder,
        config=SearchConfig(enable_bm25=True, enable_dense=False),
    )


class TestValidityFilterPipelineWiring:
    """Integration tests for ``as_of_unix`` plumbing through ``SearchPipeline.search``."""

    @pytest.mark.asyncio
    async def test_default_uses_current_time(self, monkeypatch) -> None:
        """``as_of_unix=None`` falls back to ``int(time.time())`` at call site.

        Pin time to 2025-06-15 via monkeypatch and seed one chunk valid for
        2024–2025 and one valid for 2030 only — only the 2024–2025 chunk
        should survive.
        """
        in_window = _result_with_window(
            _ts(2024, 1, 1), _ts(2025, 12, 31, 23, 59, 59), marker="now"
        )
        future = _result_with_window(_ts(2030, 1, 1), _ts(2030, 12, 31, 23, 59, 59), marker="fut")

        pipe = _make_validity_pipeline([in_window, future])
        # Patch the module-level ``time`` import that ``search()`` uses.
        # ``search`` calls ``import time; time.time()`` so we patch
        # ``time.time`` directly inside that module's import cache.
        import time as _time

        monkeypatch.setattr(_time, "time", lambda: float(_ts(2025, 6, 15)))

        results, _stats = await pipe.search("anything", top_k=5)
        assert [r.chunk.content for r in results] == ["chunk-now"]

    @pytest.mark.asyncio
    async def test_explicit_as_of_unix_filters_window(self) -> None:
        """Explicit historical ``as_of_unix`` retains chunks valid at that instant."""
        in_2024 = _result_with_window(_ts(2024, 1, 1), _ts(2024, 12, 31, 23, 59, 59), marker="2024")
        in_2025 = _result_with_window(_ts(2025, 1, 1), _ts(2025, 12, 31, 23, 59, 59), marker="2025")

        pipe = _make_validity_pipeline([in_2024, in_2025])

        results, _stats = await pipe.search("anything", top_k=5, as_of_unix=_ts(2024, 6, 15))
        assert [r.chunk.content for r in results] == ["chunk-2024"]

    @pytest.mark.asyncio
    async def test_and_with_source_filter(self) -> None:
        """Validity AND source filter — chunk must pass both to survive."""
        # Rebuild with explicit source paths so source_filter discriminates them.

        def _make(marker, source, vfrom, vto):
            chunk = Chunk(
                content=f"chunk-{marker}",
                metadata=ChunkMetadata(
                    source_file=Path(source),
                    valid_from_unix=vfrom,
                    valid_to_unix=vto,
                ),
                id=uuid.uuid4(),
                embedding=[],
            )
            return SearchResult(chunk=chunk, score=1.0, rank=1, source="fused")

        keeps_both = _make(
            "ok", "/tmp/keep/policy.md", _ts(2025, 1, 1), _ts(2025, 12, 31, 23, 59, 59)
        )
        passes_validity_fails_source = _make(
            "src-fail", "/tmp/other/policy.md", _ts(2025, 1, 1), _ts(2025, 12, 31, 23, 59, 59)
        )
        passes_source_fails_validity = _make(
            "val-fail", "/tmp/keep/expired.md", _ts(2020, 1, 1), _ts(2020, 12, 31, 23, 59, 59)
        )

        pipe = _make_validity_pipeline(
            [keeps_both, passes_validity_fails_source, passes_source_fails_validity]
        )
        results, _stats = await pipe.search(
            "anything", top_k=5, source_filter="/tmp/keep/", as_of_unix=_ts(2025, 6, 15)
        )
        assert [r.chunk.content for r in results] == ["chunk-ok"]

    @pytest.mark.asyncio
    async def test_default_path_caches_filtered_result(self, monkeypatch) -> None:
        """Two default-path calls — second hits cache (BM25 only invoked once).

        TTL expiry is not exercised here — pinning ``time.time`` to a constant
        keeps both calls inside the cache window by construction. The
        meaningful assertion is the reuse path (one storage call across two
        searches), not the TTL boundary.
        """
        import time as _time

        monkeypatch.setattr(_time, "time", lambda: float(_ts(2025, 6, 15)))

        in_window = _result_with_window(
            _ts(2024, 1, 1), _ts(2025, 12, 31, 23, 59, 59), marker="cached"
        )
        pipe = _make_validity_pipeline([in_window])

        first, _ = await pipe.search("same query", top_k=5)
        second, _ = await pipe.search("same query", top_k=5)

        assert [r.chunk.content for r in first] == ["chunk-cached"]
        assert [r.chunk.content for r in second] == ["chunk-cached"]
        # BM25 storage call invoked exactly once across two searches.
        assert pipe._storage.bm25_search.await_count == 1

    @pytest.mark.asyncio
    async def test_explicit_as_of_bypasses_cache_read_and_write(self) -> None:
        """Explicit ``as_of_unix`` bypasses both cache read and cache write.

        Read bypass: an explicit call must not be served from a previously
        cached default-path result.
        Write bypass: an explicit call must not poison the default-path
        slot — a subsequent default call still runs the retrieval.
        """
        in_window = _result_with_window(
            _ts(2024, 1, 1), _ts(2025, 12, 31, 23, 59, 59), marker="hot"
        )
        pipe = _make_validity_pipeline([in_window])

        # 1) Explicit-only call — must not write to cache.
        await pipe.search("q", top_k=5, as_of_unix=_ts(2024, 6, 15))
        assert pipe._search_cache == {}, "explicit as_of must not populate cache"

        # 2) Default call following an explicit call — must still hit storage.
        before = pipe._storage.bm25_search.await_count
        await pipe.search("q", top_k=5)
        assert pipe._storage.bm25_search.await_count == before + 1
        assert pipe._search_cache, "default-path call must populate cache"

        # 3) Default-path cache populated; an explicit call must bypass read
        #    (storage hit again, not served from default-path slot).
        before = pipe._storage.bm25_search.await_count
        await pipe.search("q", top_k=5, as_of_unix=_ts(2024, 6, 15))
        assert pipe._storage.bm25_search.await_count == before + 1, (
            "explicit as_of must bypass cache read"
        )


# ── Goal 5: mem_search(as_of=...) MCP-tool surface ─────────────────────


class TestMemSearchAsOfValidation:
    """``as_of`` parsing rejects malformed input *before* app initialization.

    Mirrors ``TestSearchValidation`` in ``test_validation_error_messages.py``:
    the early validation path runs without a real ``ctx``/app, so these tests
    invoke ``mem_search`` directly. The error message must point the caller at
    the two accepted formats so they can fix the call without consulting docs.
    """

    @pytest.mark.asyncio
    async def test_garbage_string_returns_error_with_format_hint(self) -> None:
        from memtomem.server.tools.search import mem_search

        result = await mem_search(query="hello", as_of="not-a-date")
        assert "invalid as_of" in result
        assert "not-a-date" in result
        assert "YYYY-MM-DD" in result
        assert "YYYY-QN" in result

    @pytest.mark.asyncio
    async def test_invalid_calendar_date_rejected(self) -> None:
        from memtomem.server.tools.search import mem_search

        result = await mem_search(query="hello", as_of="2025-13-01")
        assert "invalid as_of" in result
        assert "2025-13-01" in result

    @pytest.mark.asyncio
    async def test_invalid_quarter_rejected(self) -> None:
        from memtomem.server.tools.search import mem_search

        result = await mem_search(query="hello", as_of="2025-Q5")
        assert "invalid as_of" in result
        assert "2025-Q5" in result


class TestMemSearchAsOfPlumbing:
    """Valid ``as_of`` parses to unix-seconds and reaches the pipeline call.

    Stubs ``_get_app_initialized`` so the test exercises only the parse +
    forward path inside ``mem_search``, mirroring the ``TestMemExpand``
    pattern in ``test_context_window.py``. Asserts on the kwargs the tool
    passes to ``app.search_pipeline.search`` — that is the contract Goal 5
    is establishing.
    """

    @pytest.mark.asyncio
    async def test_date_as_of_passes_day_start_unix_to_pipeline(self, monkeypatch) -> None:
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, MagicMock

        from memtomem.search.pipeline import RetrievalStats
        from memtomem.server.tools import search as search_tool

        app = MagicMock()
        app.current_namespace = None
        app.search_pipeline.search = AsyncMock(return_value=([], RetrievalStats()))
        app.webhook_manager = None
        monkeypatch.setattr(search_tool, "_get_app_initialized", AsyncMock(return_value=app))

        await search_tool.mem_search(query="hi", as_of="2025-08-15", ctx=SimpleNamespace())

        kwargs = app.search_pipeline.search.await_args.kwargs
        assert kwargs["as_of_unix"] == _ts(2025, 8, 15, 0, 0, 0)

    @pytest.mark.asyncio
    async def test_quarter_as_of_passes_quarter_start_unix_to_pipeline(self, monkeypatch) -> None:
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, MagicMock

        from memtomem.search.pipeline import RetrievalStats
        from memtomem.server.tools import search as search_tool

        app = MagicMock()
        app.current_namespace = None
        app.search_pipeline.search = AsyncMock(return_value=([], RetrievalStats()))
        app.webhook_manager = None
        monkeypatch.setattr(search_tool, "_get_app_initialized", AsyncMock(return_value=app))

        await search_tool.mem_search(query="hi", as_of="2024-Q3", ctx=SimpleNamespace())

        kwargs = app.search_pipeline.search.await_args.kwargs
        # Q3 lower bound = July 1, 00:00:00 UTC.
        assert kwargs["as_of_unix"] == _ts(2024, 7, 1, 0, 0, 0)

    @pytest.mark.asyncio
    async def test_default_none_passes_none_to_pipeline(self, monkeypatch) -> None:
        """``as_of=None`` (default) keeps pipeline-level ``int(time.time())``
        fallback authoritative — the tool must not pre-resolve ``None`` to a
        timestamp, otherwise the pipeline's cache slot would diverge from the
        default-path key.
        """
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, MagicMock

        from memtomem.search.pipeline import RetrievalStats
        from memtomem.server.tools import search as search_tool

        app = MagicMock()
        app.current_namespace = None
        app.search_pipeline.search = AsyncMock(return_value=([], RetrievalStats()))
        app.webhook_manager = None
        monkeypatch.setattr(search_tool, "_get_app_initialized", AsyncMock(return_value=app))

        await search_tool.mem_search(query="hi", ctx=SimpleNamespace())

        kwargs = app.search_pipeline.search.await_args.kwargs
        assert kwargs["as_of_unix"] is None


# ── Goal 6: CLI surface (mm search --as-of, mm recall validity column) ──


def _mock_cli_components_for_search(search_return):
    """Build a mock ``cli_components()`` context manager for ``mm search``.

    ``search_return`` is the ``(results, stats)`` tuple to return from the
    pipeline call. The returned async-context-manager yields a
    ``SimpleNamespace`` whose ``search_pipeline.search`` is an AsyncMock —
    the test asserts on the AsyncMock's ``await_args``.
    """
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    pipeline_mock = AsyncMock(return_value=search_return)
    comp = SimpleNamespace(
        search_pipeline=SimpleNamespace(search=pipeline_mock),
    )

    @asynccontextmanager
    async def fake_cli_components():
        yield comp

    return fake_cli_components, pipeline_mock


def _mock_cli_components_for_recall(chunks):
    """Build a mock ``cli_components()`` context manager for ``mm recall``.

    Provides ``comp.storage.recall_chunks`` returning the supplied chunk list
    and a minimal ``comp.config.search.system_namespace_prefixes`` so the
    NamespaceFilter parse path does not blow up.
    """
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    storage = SimpleNamespace(recall_chunks=AsyncMock(return_value=list(chunks)))
    config = SimpleNamespace(search=SimpleNamespace(system_namespace_prefixes=()))
    comp = SimpleNamespace(storage=storage, config=config)

    @asynccontextmanager
    async def fake_cli_components():
        yield comp

    return fake_cli_components


def _make_chunk_for_recall(
    *,
    valid_from_unix: int | None = None,
    valid_to_unix: int | None = None,
    source: str = "/tmp/note.md",
    content: str = "hello",
) -> Chunk:
    """Construct a minimal ``Chunk`` with optional validity metadata.

    Mirrors the ``mm recall`` data shape: ``recall_chunks`` returns
    ``Chunk`` instances and the table renderer reads ``metadata.source_file``,
    ``metadata.valid_from_unix`` / ``valid_to_unix``, ``content``,
    ``created_at``.
    """
    return Chunk(
        id=uuid.uuid4(),
        content=content,
        metadata=ChunkMetadata(
            source_file=Path(source),
            valid_from_unix=valid_from_unix,
            valid_to_unix=valid_to_unix,
        ),
        created_at=datetime(2026, 4, 29, 12, 0, 0, tzinfo=timezone.utc),
    )


class TestMmSearchAsOfCLI:
    """``mm search --as-of <date>`` parses + plumbs to the pipeline.

    Mirrors the MCP-tool tests above but on the click surface. Invalid input
    travels through ``click.ClickException`` (CliRunner returns non-zero
    exit code with the message in ``result.output``) — the error message
    must include the offending value AND both accepted formats so a script
    author hitting this can correct the call without consulting docs.
    """

    def test_invalid_as_of_returns_nonzero_with_format_hint(self) -> None:
        from click.testing import CliRunner

        from memtomem.cli import cli

        result = CliRunner().invoke(cli, ["search", "--as-of", "not-a-date", "hello"])
        assert result.exit_code != 0
        assert "not-a-date" in result.output
        assert "YYYY-MM-DD" in result.output
        assert "YYYY-QN" in result.output

    def test_date_as_of_passes_day_start_unix_to_pipeline(self, monkeypatch) -> None:
        from click.testing import CliRunner

        from memtomem.cli import cli
        from memtomem.search.pipeline import RetrievalStats

        fake_components, pipeline_mock = _mock_cli_components_for_search(([], RetrievalStats()))
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", fake_components)

        result = CliRunner().invoke(cli, ["search", "--as-of", "2025-08-15", "hello"])
        assert result.exit_code == 0, result.output
        kwargs = pipeline_mock.await_args.kwargs
        assert kwargs["as_of_unix"] == _ts(2025, 8, 15, 0, 0, 0)

    def test_quarter_as_of_passes_quarter_start_unix_to_pipeline(self, monkeypatch) -> None:
        from click.testing import CliRunner

        from memtomem.cli import cli
        from memtomem.search.pipeline import RetrievalStats

        fake_components, pipeline_mock = _mock_cli_components_for_search(([], RetrievalStats()))
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", fake_components)

        result = CliRunner().invoke(cli, ["search", "--as-of", "2024-Q3", "hello"])
        assert result.exit_code == 0, result.output
        kwargs = pipeline_mock.await_args.kwargs
        assert kwargs["as_of_unix"] == _ts(2024, 7, 1, 0, 0, 0)

    def test_default_omitted_passes_none_to_pipeline(self, monkeypatch) -> None:
        from click.testing import CliRunner

        from memtomem.cli import cli
        from memtomem.search.pipeline import RetrievalStats

        fake_components, pipeline_mock = _mock_cli_components_for_search(([], RetrievalStats()))
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", fake_components)

        result = CliRunner().invoke(cli, ["search", "hello"])
        assert result.exit_code == 0, result.output
        kwargs = pipeline_mock.await_args.kwargs
        assert kwargs["as_of_unix"] is None


class TestMmRecallValidityColumn:
    """``mm recall`` table format surfaces a Validity column when at least
    one chunk in the listing has ``valid_from_unix`` or ``valid_to_unix``.

    Per RFC §CLI surfacing — the column is conditional so the default
    table stays compact for users who haven't opted into temporal-validity
    frontmatter.
    """

    def test_no_chunks_have_validity_column_omitted(self, monkeypatch) -> None:
        from click.testing import CliRunner

        from memtomem.cli import cli

        chunks = [
            _make_chunk_for_recall(content="alpha"),
            _make_chunk_for_recall(content="beta"),
        ]
        monkeypatch.setattr(
            "memtomem.cli._bootstrap.cli_components",
            _mock_cli_components_for_recall(chunks),
        )

        result = CliRunner().invoke(cli, ["recall"])
        assert result.exit_code == 0, result.output
        assert "Validity" not in result.output
        # Default columns are still rendered.
        assert "Source" in result.output
        assert "Created" in result.output

    def test_at_least_one_validity_column_shown(self, monkeypatch) -> None:
        from click.testing import CliRunner

        from memtomem.cli import cli

        chunks = [
            _make_chunk_for_recall(content="plain"),
            _make_chunk_for_recall(
                content="windowed",
                valid_from_unix=_ts(2025, 8, 15, 0, 0, 0),
                valid_to_unix=_ts(2026, 3, 31, 23, 59, 59),
            ),
        ]
        monkeypatch.setattr(
            "memtomem.cli._bootstrap.cli_components",
            _mock_cli_components_for_recall(chunks),
        )

        result = CliRunner().invoke(cli, ["recall"])
        assert result.exit_code == 0, result.output
        assert "Validity" in result.output
        # Windowed chunk renders both bounds.
        assert "[2025-08-15 → 2026-03-31]" in result.output
        # Plain chunk rendered with infinity sentinels on both sides.
        assert "[∞ → ∞]" in result.output

    def test_half_bounded_renders_infinity_sentinel(self, monkeypatch) -> None:
        """A chunk with only ``valid_from`` shows ``∞`` on the upper side."""
        from click.testing import CliRunner

        from memtomem.cli import cli

        chunks = [
            _make_chunk_for_recall(
                content="lower-only",
                valid_from_unix=_ts(2025, 8, 15, 0, 0, 0),
            ),
        ]
        monkeypatch.setattr(
            "memtomem.cli._bootstrap.cli_components",
            _mock_cli_components_for_recall(chunks),
        )

        result = CliRunner().invoke(cli, ["recall"])
        assert result.exit_code == 0, result.output
        assert "[2025-08-15 → ∞]" in result.output
