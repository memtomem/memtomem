"""Tests for search/dedup.py DedupScanner — exact + near duplicate detection."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from memtomem.models import Chunk, ChunkMetadata, SearchResult
from memtomem.search.dedup import DedupScanner


def _mk(content: str, created_at: datetime | None = None, embedding: list[float] | None = None):
    return Chunk(
        content=content,
        metadata=ChunkMetadata(source_file=Path("/s.md")),
        embedding=embedding or [],
        created_at=created_at or datetime.now(timezone.utc),
    )


class _FakeStorage:
    """Minimal async storage stub. dense_search is driven by content match
    rather than real cosine so tests can assert threshold behavior precisely."""

    def __init__(
        self, chunks: list[Chunk], similarities: dict[tuple[str, str], float] | None = None
    ):
        # similarities keyed by (query_content, candidate_content). Missing pair => 0.0.
        self._chunks = chunks
        self._similarities = similarities or {}

    async def get_all_source_files(self) -> list[Path]:
        return [Path("/s.md")] if self._chunks else []

    async def list_chunks_by_source(self, source: Path, limit: int) -> list[Chunk]:
        return self._chunks[:limit]

    async def dense_search(self, embedding: list[float], top_k: int) -> list[SearchResult]:
        # embedding[0] carries the "query content hash" so we can look up canned scores.
        # We encode the query chunk's content in embedding[0] via id lookup below.
        query_marker = self._query_marker
        results: list[SearchResult] = []
        for rank, c in enumerate(self._chunks, start=1):
            score = self._similarities.get((query_marker, c.content), 0.0)
            if score > 0.0:
                results.append(SearchResult(chunk=c, score=score, rank=rank, source="dense"))
        results.sort(key=lambda r: -r.score)
        return results[:top_k]

    _query_marker: str = ""


class _FakeEmbedder:
    """Record which content each embedding corresponds to so _FakeStorage can
    route dense_search to the right canned similarity row."""

    def __init__(self, storage: _FakeStorage):
        self._storage = storage
        self._next_batch: list[str] = []

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        # Return dummy vectors; stash each query content on the storage so
        # the next dense_search call can look it up (works because scan()
        # alternates embed -> dense_search per chunk, not in parallel).
        self._next_batch = list(texts)
        return [[1.0, 0.0, 0.0] for _ in texts]


@pytest.fixture
def scanner_factory():
    def _make(chunks: list[Chunk], similarities: dict[tuple[str, str], float] | None = None):
        storage = _FakeStorage(chunks, similarities)
        embedder = _FakeEmbedder(storage)

        # Patch embed_texts so each subsequent dense_search call sees the
        # right _query_marker.
        original_embed = embedder.embed_texts
        original_dense = storage.dense_search

        async def wrapped_embed(texts: list[str]) -> list[list[float]]:
            storage._pending_markers = list(texts)  # type: ignore[attr-defined]
            return await original_embed(texts)

        async def wrapped_dense(embedding: list[float], top_k: int) -> list[SearchResult]:
            pending = getattr(storage, "_pending_markers", [])
            if pending:
                storage._query_marker = pending.pop(0)
            return await original_dense(embedding, top_k)

        embedder.embed_texts = wrapped_embed  # type: ignore[method-assign]
        storage.dense_search = wrapped_dense  # type: ignore[method-assign]

        return DedupScanner(storage, embedder), storage, embedder

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
