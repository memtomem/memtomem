"""Tests for search/dedup.py DedupScanner — exact + near duplicate detection."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from memtomem.models import (
    ORIGIN_CONSOLIDATION_POLICY,
    Chunk,
    ChunkMetadata,
    SearchResult,
)
from memtomem.search.dedup import DedupScanner


def _mk(content: str, created_at: datetime | None = None, embedding: list[float] | None = None):
    return Chunk(
        content=content,
        metadata=ChunkMetadata(source_file=Path("/s.md")),
        embedding=embedding or [],
        created_at=created_at or datetime.now(timezone.utc),
    )


class _FakeStorage:
    """Minimal async storage stub with stored vectors and canned dense scores.

    Each vectorised chunk's stored vector is ``[float(index)]``, so
    ``dense_search`` can tell which chunk is probing and look up a canned score
    by content. Chunks listed in ``without_vector`` have no stored vector: the
    lookup omits them and they never appear as dense neighbours, like a row
    missing from ``chunks_vec``."""

    def __init__(
        self,
        chunks: list[Chunk],
        similarities: dict[tuple[str, str], float] | None = None,
        without_vector: set[UUID] | None = None,
        dense_enabled: bool = True,
        held_sources: set[Path] | None = None,
    ):
        # similarities keyed by (query_content, candidate_content). Missing pair => 0.0.
        self._chunks = chunks
        self._similarities = similarities or {}
        self._without_vector = without_vector or set()
        self.dense_enabled = dense_enabled
        self.held_sources = held_sources or set()
        self.lookups: list[list[str]] = []
        self.probes: list[dict] = []

    async def get_all_source_files(self) -> list[Path]:
        return sorted({chunk.metadata.source_file for chunk in self._chunks})

    async def list_chunks_by_source(self, source: Path, limit: int) -> list[Chunk]:
        return [chunk for chunk in self._chunks if chunk.metadata.source_file == source][:limit]

    async def is_source_held(self, source: Path) -> bool:
        return source in self.held_sources

    async def get_chunks_batch(self, ids: list[UUID]) -> dict[UUID, Chunk]:
        by_id = {c.id: c for c in self._chunks}
        return {i: by_id[i] for i in ids if i in by_id}

    async def upsert_chunks(self, chunks: list[Chunk]) -> None:
        by_id = {c.id: i for i, c in enumerate(self._chunks)}
        for c in chunks:
            if c.id in by_id:
                self._chunks[by_id[c.id]] = c
            else:
                self._chunks.append(c)

    async def delete_chunks(self, ids: list[UUID]) -> int:
        target = set(ids)
        before = len(self._chunks)
        self._chunks[:] = [c for c in self._chunks if c.id not in target]
        return before - len(self._chunks)

    def _vector_of(self, chunk: Chunk) -> list[float]:
        return [float(self._chunks.index(chunk))]

    async def get_embeddings_for_chunks(self, chunk_ids: list[str]) -> dict[str, list[float]]:
        self.lookups.append(list(chunk_ids))
        return {
            str(c.id): self._vector_of(c)
            for c in self._chunks
            if str(c.id) in chunk_ids and c.id not in self._without_vector
        }

    async def dense_search(
        self,
        embedding: list[float],
        top_k: int,
        **kwargs,
    ) -> list[SearchResult]:
        # ``project_context_root`` is recorded, not applied — the fake doesn't
        # filter by scope; production real-backend tests cover the threading.
        query = self._chunks[int(embedding[0])]
        self.probes.append({"chunk": query, "embedding": list(embedding), **kwargs})
        results: list[SearchResult] = []
        for rank, c in enumerate(self._chunks, start=1):
            if c.id in self._without_vector:
                continue
            score = 1.0 if c is query else self._similarities.get((query.content, c.content), 0.0)
            if score > 0.0:
                results.append(SearchResult(chunk=c, score=score, rank=rank, source="dense"))
        results.sort(key=lambda r: -r.score)
        return results[:top_k]


@pytest.fixture
def scanner_factory():
    def _make(
        chunks: list[Chunk],
        similarities: dict[tuple[str, str], float] | None = None,
        **storage_kwargs,
    ):
        storage = _FakeStorage(chunks, similarities, **storage_kwargs)
        return DedupScanner(storage), storage

    return _make


class TestExactDuplicates:
    @pytest.mark.asyncio
    async def test_detects_two_chunks_with_same_content_hash(self, scanner_factory):
        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        older = _mk("identical text", created_at=t0)
        newer = _mk("identical text", created_at=t0 + timedelta(minutes=5))
        assert older.content_hash == newer.content_hash

        scanner, *_ = scanner_factory([older, newer])
        result = await scanner.scan()

        assert len(result) == 1
        cand = result[0]
        assert cand.exact is True
        assert cand.score == 1.0
        # Older becomes chunk_a (keep candidate).
        assert cand.chunk_a.id == older.id
        assert cand.chunk_b.id == newer.id

    @pytest.mark.asyncio
    async def test_no_duplicates_when_all_content_differs(self, scanner_factory):
        chunks = [_mk("a"), _mk("b"), _mk("c")]
        scanner, *_ = scanner_factory(chunks)

        result = await scanner.scan()

        assert result == []

    @pytest.mark.asyncio
    async def test_held_source_is_excluded_and_cannot_be_a_merge_target(self, scanner_factory):
        held = _mk("same text")
        visible = replace(
            _mk("same text"),
            metadata=replace(held.metadata, source_file=Path("/visible.md")),
        )
        scanner, storage = scanner_factory(
            [held, visible], held_sources={held.metadata.source_file}
        )

        assert await scanner.scan() == []
        with pytest.raises(ValueError, match="held source"):
            await scanner.merge(held.id, [visible.id], dry_run=False)
        assert {chunk.id for chunk in storage._chunks} == {held.id, visible.id}


class TestNearDuplicates:
    @pytest.mark.asyncio
    async def test_detects_pair_above_threshold(self, scanner_factory):
        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        a = _mk("deploy to prod on friday", created_at=t0)
        b = _mk("ship to production friday", created_at=t0 + timedelta(hours=1))

        similarities = {
            ("deploy to prod on friday", "ship to production friday"): 0.95,
            ("ship to production friday", "deploy to prod on friday"): 0.95,
        }
        scanner, *_ = scanner_factory([a, b], similarities)

        result = await scanner.scan(threshold=0.92)

        assert len(result) == 1
        cand = result[0]
        assert cand.exact is False
        assert cand.score == pytest.approx(0.95)
        # Older becomes chunk_a.
        assert cand.chunk_a.id == a.id

    @pytest.mark.asyncio
    async def test_below_threshold_is_not_flagged(self, scanner_factory):
        a = _mk("one thing")
        b = _mk("another thing")
        similarities = {
            ("one thing", "another thing"): 0.80,
            ("another thing", "one thing"): 0.80,
        }
        scanner, *_ = scanner_factory([a, b], similarities)

        result = await scanner.scan(threshold=0.92)

        assert result == []


class TestEdgeCases:
    @pytest.mark.asyncio
    async def test_empty_store_returns_no_candidates(self, scanner_factory):
        scanner, *_ = scanner_factory([])

        result = await scanner.scan()

        assert result == []


class TestOrdering:
    @pytest.mark.asyncio
    async def test_exact_duplicates_come_before_near_duplicates(self, scanner_factory):
        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        # Exact pair (content_hash match).
        ex_a = _mk("same", created_at=t0)
        ex_b = _mk("same", created_at=t0 + timedelta(minutes=1))
        # Near pair with high similarity but different content.
        near_a = _mk("deploy friday", created_at=t0 + timedelta(hours=2))
        near_b = _mk("ship friday", created_at=t0 + timedelta(hours=3))

        similarities = {
            ("deploy friday", "ship friday"): 0.95,
            ("ship friday", "deploy friday"): 0.95,
        }
        scanner, *_ = scanner_factory([ex_a, ex_b, near_a, near_b], similarities)

        result = await scanner.scan(threshold=0.92)

        assert len(result) == 2
        assert result[0].exact is True  # exact first
        assert result[1].exact is False  # near second

    @pytest.mark.asyncio
    async def test_near_duplicates_sorted_by_score_descending(self, scanner_factory):
        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        a = _mk("alpha", created_at=t0)
        b = _mk("beta", created_at=t0 + timedelta(minutes=1))
        c = _mk("gamma", created_at=t0 + timedelta(minutes=2))

        similarities: dict[tuple[str, str], float] = {
            ("alpha", "beta"): 0.93,
            ("beta", "alpha"): 0.93,
            ("alpha", "gamma"): 0.99,
            ("gamma", "alpha"): 0.99,
        }
        scanner, *_ = scanner_factory([a, b, c], similarities)

        result = await scanner.scan(threshold=0.92)

        assert len(result) == 2
        # 0.99 pair should come before 0.93 pair.
        assert result[0].score == pytest.approx(0.99)
        assert result[1].score == pytest.approx(0.93)


class TestStoredVectorsAndCoverage:
    """The near phase searches with stored vectors and reports what it covered.

    Re-embedding ``content`` did not reproduce the stored ``retrieval_content``
    vectors, so these pin that the stored vector itself reaches ``dense_search``
    and that chunks without one are counted instead of probed."""

    @pytest.mark.asyncio
    async def test_each_chunk_probes_with_its_stored_vector(self, scanner_factory):
        a, b = _mk("alpha"), _mk("beta")
        a = replace(a, metadata=replace(a.metadata, project_root=Path("/proj-a")))
        b = replace(b, metadata=replace(b.metadata, project_root=Path("/proj-b")))
        scanner, storage = scanner_factory([a, b])

        await scanner.scan()

        assert storage.lookups == [[str(a.id), str(b.id)]]
        assert [(p["chunk"].id, p["embedding"]) for p in storage.probes] == [
            (a.id, [0.0]),
            (b.id, [1.0]),
        ]
        assert [p["project_context_root"] for p in storage.probes] == [
            Path("/proj-a"),
            Path("/proj-b"),
        ]

    @pytest.mark.asyncio
    async def test_chunk_without_vector_is_counted_not_probed(self, scanner_factory):
        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        pending = _mk("not indexed yet", created_at=t0)
        a = _mk("deploy friday", created_at=t0 + timedelta(minutes=1))
        b = _mk("ship friday", created_at=t0 + timedelta(minutes=2))
        similarities = {
            ("deploy friday", "ship friday"): 0.95,
            ("ship friday", "deploy friday"): 0.95,
        }
        scanner, storage = scanner_factory(
            [pending, a, b], similarities, without_vector={pending.id}
        )

        candidates, coverage = await scanner.scan_with_coverage(threshold=0.92)

        assert [(c.chunk_a.id, c.chunk_b.id) for c in candidates] == [(a.id, b.id)]
        assert [p["chunk"].id for p in storage.probes] == [a.id, b.id]
        assert (coverage.pool, coverage.probed, coverage.without_vector) == (3, 2, 1)
        assert coverage.near_search_enabled is True

    @pytest.mark.asyncio
    async def test_exact_duplicates_survive_a_pool_with_no_vectors(self, scanner_factory):
        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        older = _mk("same text", created_at=t0)
        newer = _mk("same text", created_at=t0 + timedelta(minutes=1))
        scanner, storage = scanner_factory([older, newer], without_vector={older.id, newer.id})

        candidates, coverage = await scanner.scan_with_coverage()

        assert [(c.chunk_a.id, c.exact) for c in candidates] == [(older.id, True)]
        assert storage.probes == []
        assert (coverage.pool, coverage.probed, coverage.without_vector) == (2, 0, 2)

    @pytest.mark.asyncio
    async def test_bm25_only_store_skips_the_lookup(self, scanner_factory):
        scanner, storage = scanner_factory([_mk("a"), _mk("b")], dense_enabled=False)

        candidates, coverage = await scanner.scan_with_coverage()

        assert candidates == []
        assert storage.lookups == [] and storage.probes == []
        assert coverage.near_search_enabled is False
        assert (coverage.pool, coverage.probed, coverage.without_vector) == (2, 0, 0)

    @pytest.mark.asyncio
    async def test_pool_is_bounded_by_max_scan(self, scanner_factory):
        chunks = [_mk(f"c{i}") for i in range(5)]
        scanner, storage = scanner_factory(chunks)

        _, coverage = await scanner.scan_with_coverage(max_scan=3)

        assert coverage.pool == 3
        assert storage.lookups == [[str(c.id) for c in chunks[:3]]]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("max_scan", [0, -1])
    async def test_non_positive_max_scan_is_rejected(self, scanner_factory, max_scan):
        scanner, storage = scanner_factory([_mk("a"), _mk("b"), _mk("c")])

        with pytest.raises(ValueError, match="max_scan"):
            await scanner.scan_with_coverage(max_scan=max_scan)
        assert storage.lookups == []

    @pytest.mark.asyncio
    async def test_scan_returns_the_same_candidates(self, scanner_factory):
        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        chunks = [_mk("same", created_at=t0), _mk("same", created_at=t0 + timedelta(seconds=1))]
        scanner, _ = scanner_factory(chunks)

        assert await scanner.scan() == (await scanner.scan_with_coverage())[0]


class TestMcpScanCoverage:
    """``mem_dedup_scan`` says what it covered on both branches."""

    @staticmethod
    def _run(monkeypatch, candidates, coverage):
        from memtomem.search.dedup import DedupCoverage
        from memtomem.server.tools import dedup_decay

        app = MagicMock()
        app.dedup_scanner.scan_with_coverage = AsyncMock(
            return_value=(candidates, DedupCoverage(**coverage))
        )

        async def _fake_app(_ctx):
            return app

        monkeypatch.setattr(dedup_decay, "_get_app_initialized", _fake_app)
        return dedup_decay, app

    @pytest.mark.asyncio
    async def test_empty_result_names_unprobed_chunks(self, monkeypatch):
        tool, _ = self._run(
            monkeypatch,
            [],
            {"pool": 5, "near_search_enabled": True, "probed": 3, "without_vector": 2},
        )

        out = await tool.mem_dedup_scan(ctx=None)

        assert out == (
            "No duplicate chunks found (threshold=0.92).\n"
            "Scanned 5 chunks; near-duplicate search probed 3, "
            "2 had no usable stored vector and were not probed."
        )

    @pytest.mark.asyncio
    async def test_candidates_end_with_the_coverage_line(self, monkeypatch):
        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        from memtomem.search.dedup import DedupCandidate

        pair = DedupCandidate(
            chunk_a=_mk("x", created_at=t0), chunk_b=_mk("x"), score=1.0, exact=True
        )
        tool, _ = self._run(
            monkeypatch,
            [pair],
            {"pool": 2, "near_search_enabled": True, "probed": 2, "without_vector": 0},
        )

        out = await tool.mem_dedup_scan(ctx=None)

        assert out.splitlines()[0] == "Duplicate candidates: 1 pairs (threshold=0.92):"
        assert out.splitlines()[-1] == "Scanned 2 chunks; near-duplicate search probed 2."

    @pytest.mark.asyncio
    async def test_bm25_only_store_says_near_search_is_off(self, monkeypatch):
        tool, _ = self._run(
            monkeypatch,
            [],
            {"pool": 4, "near_search_enabled": False, "probed": 0, "without_vector": 0},
        )

        out = await tool.mem_dedup_scan(ctx=None)

        assert out.splitlines()[-1] == (
            "Scanned 4 chunks; near-duplicate search is off (BM25-only store)."
        )

    @pytest.mark.asyncio
    async def test_non_positive_max_scan_is_an_error_before_scanning(self, monkeypatch):
        tool, app = self._run(
            monkeypatch,
            [],
            {"pool": 0, "near_search_enabled": True, "probed": 0, "without_vector": 0},
        )

        out = await tool.mem_dedup_scan(max_scan=0, ctx=None)

        assert out == "Error: max_scan must be at least 1, got 0."
        app.dedup_scanner.scan_with_coverage.assert_not_awaited()


class TestMergeDryRun:
    """``DedupScanner.merge`` safety-default parity with decay/cleanup tools.

    The MCP wrapper ``mem_dedup_merge`` defaults ``dry_run=True`` so a
    wrong UUID in a maintenance script can't silently destroy data on
    first call, mirroring ``mem_decay_expire`` and ``mem_cleanup_orphans``.
    The scanner-side default stays ``False`` because the web route
    (``POST /api/dedup/merge``) calls through after the user has already
    confirmed against a visible scan preview — the safety gate lives at
    the MCP layer for the no-preview path.
    """

    @pytest.mark.asyncio
    async def test_dry_run_returns_count_without_deleting(self, scanner_factory):
        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        keep = _mk("identical text", created_at=t0)
        dup_a = _mk("identical text", created_at=t0 + timedelta(minutes=1))
        dup_b = _mk("identical text", created_at=t0 + timedelta(minutes=2))
        scanner, storage = scanner_factory([keep, dup_a, dup_b])

        would_delete = await scanner.merge(keep.id, [dup_a.id, dup_b.id], dry_run=True)

        assert would_delete == 2
        # Storage still holds all three chunks.
        assert {c.id for c in storage._chunks} == {keep.id, dup_a.id, dup_b.id}

    @pytest.mark.asyncio
    async def test_dry_run_skips_tag_merge_upsert(self, scanner_factory):
        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        keep = Chunk(
            content="x",
            metadata=ChunkMetadata(source_file=Path("/s.md"), tags=("a",)),
            embedding=[],
            created_at=t0,
        )
        dup = Chunk(
            content="x",
            metadata=ChunkMetadata(source_file=Path("/s.md"), tags=("b",)),
            embedding=[],
            created_at=t0 + timedelta(minutes=1),
        )
        scanner, storage = scanner_factory([keep, dup])

        await scanner.merge(keep.id, [dup.id], dry_run=True)

        # keep_chunk's tags are unchanged — no upsert happened.
        kept = next(c for c in storage._chunks if c.id == keep.id)
        assert kept.metadata.tags == ("a",)

    @pytest.mark.asyncio
    async def test_apply_deletes_and_merges_tags(self, scanner_factory):
        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        keep = Chunk(
            content="x",
            metadata=ChunkMetadata(source_file=Path("/s.md"), tags=("a",)),
            embedding=[],
            created_at=t0,
        )
        dup = Chunk(
            content="x",
            metadata=ChunkMetadata(source_file=Path("/s.md"), tags=("b",)),
            embedding=[],
            created_at=t0 + timedelta(minutes=1),
        )
        scanner, storage = scanner_factory([keep, dup])

        deleted = await scanner.merge(keep.id, [dup.id], dry_run=False)

        assert deleted == 1
        assert {c.id for c in storage._chunks} == {keep.id}
        kept = storage._chunks[0]
        assert kept.metadata.tags == ("a", "b")

    @pytest.mark.asyncio
    async def test_tag_merge_preserves_every_other_metadata_field(self, scanner_factory):
        """The merge rewrites tags — it must not quietly drop the rest.

        This rebuilt ``ChunkMetadata`` field by field, so each field added
        since it was written was silently reset on any tag-merge. ``origin``
        made that load-bearing (#2161): losing it leaves a consolidation
        summary unowned, and the policy then fails every later regeneration of
        that source closed rather than replacing it.

        Mutation check: enumerating the fields again fails this test."""
        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        rich = ChunkMetadata(
            source_file=Path("/s.md"),
            tags=("a",),
            origin=ORIGIN_CONSOLIDATION_POLICY,
            scope="project_shared",
            project_root=Path("/repo"),
            valid_from_unix=1_700_000_000,
            valid_to_unix=1_800_000_000,
            overlap_before=7,
            overlap_after=9,
            parent_context="Doc title",
            file_context="s.md > Doc title",
        )
        keep = Chunk(content="x", metadata=rich, embedding=[], created_at=t0)
        dup = Chunk(
            content="x",
            metadata=ChunkMetadata(source_file=Path("/s.md"), tags=("b",)),
            embedding=[],
            created_at=t0 + timedelta(minutes=1),
        )
        scanner, storage = scanner_factory([keep, dup])

        await scanner.merge(keep.id, [dup.id], dry_run=False)

        kept = storage._chunks[0].metadata
        assert kept.tags == ("a", "b")
        assert kept == replace(rich, tags=("a", "b"))

    @pytest.mark.asyncio
    async def test_default_apply_path_unchanged(self, scanner_factory):
        # Web route at ``POST /api/dedup/merge`` calls
        # ``scanner.merge(keep, deletes)`` without the new ``dry_run`` kwarg.
        # Pin the default behavior so that path stays write-through.
        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        keep = _mk("dup", created_at=t0)
        dup = _mk("dup", created_at=t0 + timedelta(minutes=1))
        scanner, storage = scanner_factory([keep, dup])

        deleted = await scanner.merge(keep.id, [dup.id])

        assert deleted == 1
        assert {c.id for c in storage._chunks} == {keep.id}


class TestMcpMergeInvalidation:
    """``mem_dedup_merge`` and the web ``/dedup/merge`` route must agree on
    when the search cache is dropped (#2157)."""

    @staticmethod
    def _app() -> MagicMock:
        app = MagicMock()
        app.dedup_scanner = MagicMock()
        app.search_pipeline = MagicMock()
        return app

    @staticmethod
    def _patch(monkeypatch, app):
        from memtomem.server.tools import dedup_decay

        async def _fake_app(_ctx):
            return app

        monkeypatch.setattr(dedup_decay, "_get_app_initialized", _fake_app)

        async def _no_provenance(*_args, **_kwargs):
            return None

        monkeypatch.setattr(dedup_decay, "capture_session_for_untracked_write", _no_provenance)
        monkeypatch.setattr(dedup_decay, "flag_untracked_write", _no_provenance)
        return dedup_decay

    @pytest.mark.asyncio
    async def test_failed_apply_invalidates(self, monkeypatch):
        """The kept chunk's merged tags are upserted before the losers are
        deleted, so a failed apply may already be search-visible."""
        app = self._app()
        app.dedup_scanner.merge = AsyncMock(side_effect=RuntimeError("delete failed"))
        dedup_decay = self._patch(monkeypatch, app)

        out = await dedup_decay.mem_dedup_merge(
            keep_id=str(uuid4()), delete_ids=[str(uuid4())], dry_run=False, ctx=None
        )

        assert "delete failed" in out
        app.search_pipeline.invalidate_cache.assert_called_once()

    @pytest.mark.asyncio
    async def test_failed_dry_run_does_not_invalidate(self, monkeypatch):
        """A preview writes nothing, however it ends."""
        app = self._app()
        app.dedup_scanner.merge = AsyncMock(side_effect=RuntimeError("scan failed"))
        dedup_decay = self._patch(monkeypatch, app)

        out = await dedup_decay.mem_dedup_merge(
            keep_id=str(uuid4()), delete_ids=[str(uuid4())], dry_run=True, ctx=None
        )

        assert "scan failed" in out
        app.search_pipeline.invalidate_cache.assert_not_called()
