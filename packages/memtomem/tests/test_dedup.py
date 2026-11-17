"""Tests for search/dedup.py DedupScanner — exact + near duplicate detection."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from itertools import count
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
from memtomem.search.dedup import DedupScanner, parse_scan_cursor
from memtomem.storage.base import VisiblePool


_next_id = count(1)


def _mk(
    content: str,
    created_at: datetime | None = None,
    embedding: list[float] | None = None,
    id: UUID | None = None,
):
    # Ids ascend in creation order, so the scan's id-ordered pool is the order a
    # test lists its chunks in unless the test passes ids to say otherwise.
    return Chunk(
        content=content,
        metadata=ChunkMetadata(source_file=Path("/s.md")),
        id=id or UUID(int=next(_next_id)),
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
        self.transactions = 0
        self.transaction_waits: list[float] = []
        self.finalized: list[bool] = []
        self.batch_reads: list[list[UUID]] = []
        self.deletes: list[list[UUID]] = []
        self.lookups: list[list[str]] = []
        self.probes: list[dict] = []
        self.cursors: list[str | None] = []

    async def list_visible_chunks_by_id(self, limit: int, after: str | None = None) -> VisiblePool:
        # The real read screens holds, orders by id and applies the cursor in
        # one statement; mirror all three. There is no per-source listing here
        # for a scanner to fall back on.
        self.cursors.append(after)
        visible = sorted(
            (c for c in self._chunks if c.metadata.source_file not in self.held_sources),
            key=lambda c: str(c.id),
        )
        above = [c for c in visible if after is None or str(c.id) > after]
        return VisiblePool(chunks=above[:limit], visible=len(visible), above_cursor=len(above))

    async def is_source_hidden(self, source: Path) -> bool:
        return source in self.held_sources

    @asynccontextmanager
    async def transaction(self, *, wait: float = 0.0):
        self.transactions += 1
        self.transaction_waits.append(wait)
        yield

    async def get_chunks_batch(self, ids: list[UUID]) -> dict[UUID, Chunk]:
        self.batch_reads.append(list(ids))
        by_id = {c.id: c for c in self._chunks}
        return {i: by_id[i] for i in ids if i in by_id}

    async def upsert_chunks(self, chunks: list[Chunk]) -> None:
        by_id = {c.id: i for i, c in enumerate(self._chunks)}
        for c in chunks:
            if c.id in by_id:
                self._chunks[by_id[c.id]] = c
            else:
                self._chunks.append(c)

    async def delete_chunks(self, ids: list[UUID], *, finalize_emptied: bool = False) -> int:
        self.finalized.append(finalize_emptied)
        self.deletes.append(list(ids))
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

        assert (coverage.pool, coverage.eligible) == (3, 5)
        assert storage.lookups == [[str(c.id) for c in chunks[:3]]]

    @pytest.mark.asyncio
    async def test_pool_is_the_lowest_ids_whatever_order_the_chunks_were_written_in(
        self, scanner_factory
    ):
        chunks = [_mk(f"c{n}", id=UUID(int=n)) for n in (50, 10, 40, 20, 30)]
        scanner, storage = scanner_factory(chunks)

        _, coverage = await scanner.scan_with_coverage(max_scan=2)

        assert (coverage.pool, coverage.eligible) == (2, 5)
        assert storage.lookups == [[str(UUID(int=10)), str(UUID(int=20))]]

    @pytest.mark.asyncio
    async def test_a_held_source_is_in_neither_the_pool_nor_the_count(self, scanner_factory):
        held = replace(_mk("held"), metadata=ChunkMetadata(source_file=Path("/held.md")))
        chunks = [held, _mk("a"), _mk("b")]
        scanner, storage = scanner_factory(chunks, held_sources={Path("/held.md")})

        _, coverage = await scanner.scan_with_coverage(max_scan=5)

        assert (coverage.pool, coverage.eligible) == (2, 2)
        assert storage.lookups == [[str(c.id) for c in chunks[1:]]]

    @pytest.mark.asyncio
    async def test_candidates_do_not_depend_on_the_order_chunks_were_written_in(
        self, scanner_factory
    ):
        """Pool order feeds three tie-breaks; none may let write order through.

        The set hits each: an exact group with tied timestamps and one later
        member, near pairs with tied timestamps, a near pair only one side
        discovers, and more equal-score pairs than ``limit`` keeps. No winner is
        asserted, only that two write orders of one set give one result.
        """
        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        later = t0 + timedelta(minutes=1)
        chunks = [
            _mk("same body", created_at=t0, id=UUID(int=7)),
            _mk("same body", created_at=t0, id=UUID(int=3)),
            _mk("same body", created_at=t0, id=UUID(int=5)),
            _mk("same body", created_at=later, id=UUID(int=1)),
            _mk("near one", created_at=t0, id=UUID(int=8)),
            _mk("near two", created_at=t0, id=UUID(int=2)),
            _mk("one-way from", created_at=t0, id=UUID(int=9)),
            _mk("one-way to", created_at=t0, id=UUID(int=4)),
            _mk("near three", created_at=t0, id=UUID(int=6)),
        ]
        similarities = {
            ("near one", "near two"): 0.95,
            ("near two", "near one"): 0.95,
            ("near one", "near three"): 0.95,
            ("near three", "near one"): 0.95,
            # Only "one-way from" finds its neighbour, as a project chunk finds
            # a user-tier one that cannot find it back.
            ("one-way from", "one-way to"): 0.95,
        }

        async def scan(order: list[Chunk], limit: int):
            scanner, _ = scanner_factory(order, similarities)
            found = await scanner.scan(threshold=0.92, limit=limit)
            return [(c.chunk_a.id, c.chunk_b.id, c.exact, c.score) for c in found]

        written = list(chunks)
        rewritten = list(reversed(chunks))
        full = await scan(written, limit=100)
        assert len(full) == 6  # 3 exact pairs and 3 near pairs: every tie-break is exercised
        assert await scan(rewritten, limit=100) == full
        for limit in range(1, 6):
            assert await scan(written, limit=limit) == await scan(rewritten, limit=limit)

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

    @pytest.mark.asyncio
    async def test_scan_takes_the_cursor_too(self, scanner_factory):
        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        first, same, also_same = (
            _mk(body, created_at=t0 + timedelta(seconds=i))
            for i, body in enumerate(("other", "same", "same"))
        )
        scanner, _ = scanner_factory([first, same, also_same], dense_enabled=False)

        assert await scanner.scan(max_scan=2) == []
        after_the_first = await scanner.scan(max_scan=2, after=first.id)

        assert [(c.chunk_a.id, c.chunk_b.id) for c in after_the_first] == [(same.id, also_same.id)]


def _ro(content: str, source: str = "/data.json", **kwargs) -> Chunk:
    """A chunk stored read-only, as ``chunk_json`` stamps every slice."""
    chunk = _mk(content, **kwargs)
    chunk.metadata = replace(chunk.metadata, source_file=Path(source), source_read_only=True)
    return chunk


def _rw(content: str, source: str = "/data.json", **kwargs) -> Chunk:
    chunk = _mk(content, **kwargs)
    chunk.metadata = replace(chunk.metadata, source_file=Path(source))
    return chunk


def _both_ways(a: str, b: str, score: float) -> dict[tuple[str, str], float]:
    return {(a, b): score, (b, a): score}


class TestSameSourceReadOnlyPairs:
    """#3418: two read-only chunks of one source are not a candidate."""

    @pytest.mark.asyncio
    async def test_exact_pair_is_left_out_and_counted(self, scanner_factory):
        scanner, _ = scanner_factory([_ro("{slice}"), _ro("{slice}")])

        candidates, coverage = await scanner.scan_with_coverage()

        assert candidates == []
        assert coverage.same_source_read_only == 1

    @pytest.mark.asyncio
    async def test_near_pair_is_left_out_and_counted(self, scanner_factory):
        scanner, _ = scanner_factory(
            [_ro('{"a": 1'), _ro('{"a": 2')], _both_ways('{"a": 1', '{"a": 2', 0.97)
        )

        candidates, coverage = await scanner.scan_with_coverage()

        assert candidates == []
        assert coverage.same_source_read_only == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("make_a", "make_b"),
        [
            # Both orders: the older chunk is chunk_a in either phase.
            pytest.param(lambda c: _ro(c), lambda c: _rw(c), id="same-file-older-read-only"),
            pytest.param(lambda c: _rw(c), lambda c: _ro(c), id="same-file-older-writable"),
            pytest.param(lambda c: _ro(c), lambda c: _ro(c, source="/other.json"), id="two-files"),
            pytest.param(lambda c: _rw(c), lambda c: _rw(c), id="same-file-writable"),
        ],
    )
    @pytest.mark.parametrize("exact", [True, False], ids=["exact", "near"])
    async def test_other_pairs_are_kept(self, scanner_factory, make_a, make_b, exact):
        a = make_a("one text")
        b = make_b("one text" if exact else "one txt")
        sims = {} if exact else _both_ways("one text", "one txt", 0.97)
        scanner, _ = scanner_factory([a, b], sims)

        candidates, coverage = await scanner.scan_with_coverage()

        assert [(c.chunk_a.id, c.chunk_b.id, c.exact) for c in candidates] == [(a.id, b.id, exact)]
        assert coverage.same_source_read_only == 0

    @pytest.mark.asyncio
    async def test_a_pair_both_phases_and_both_probes_reach_is_counted_once(self, scanner_factory):
        # Exact (one hash) and near (each probes the other at 0.99): the exact
        # phase, then each member's near probe, all reach the one pair.
        scanner, storage = scanner_factory(
            [_ro("{slice}"), _ro("{slice}")], {("{slice}", "{slice}"): 0.99}
        )

        candidates, coverage = await scanner.scan_with_coverage()

        assert candidates == []
        assert len(storage.probes) == 2
        assert coverage.same_source_read_only == 1

    @pytest.mark.asyncio
    async def test_bm25_only_store_counts_exact_pairs(self, scanner_factory):
        scanner, _ = scanner_factory([_ro("{slice}"), _ro("{slice}")], dense_enabled=False)

        candidates, coverage = await scanner.scan_with_coverage()

        assert candidates == []
        assert coverage.near_search_enabled is False
        assert coverage.same_source_read_only == 1

    @pytest.mark.asyncio
    async def test_left_out_pairs_take_no_limit_slot_and_are_all_counted(self, scanner_factory):
        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        kept_a = _rw("note", source="/a.md", created_at=t0)
        kept_b = _rw("note", source="/b.md", created_at=t0 + timedelta(hours=1))
        # All three pairs are exact (score 1.0), so they keep pool order, and
        # these lower ids would take the one slot first.
        left = [_ro("{x}"), _ro("{x}"), _ro("{y}"), _ro("{y}")]
        scanner, _ = scanner_factory([*left, kept_a, kept_b])

        candidates, coverage = await scanner.scan_with_coverage(limit=1)

        assert [(c.chunk_a.id, c.chunk_b.id) for c in candidates] == [(kept_a.id, kept_b.id)]
        assert coverage.same_source_read_only == 2

    @pytest.mark.asyncio
    async def test_siblings_still_fill_a_probes_neighbour_slots(self, scanner_factory):
        # Stated limit: the filter runs after each probe's top 6, so a probe
        # whose slots are all left-out siblings does not reach a writable
        # neighbour past them. Not widened: on a real store copy (2026-11-17,
        # max_scan=5000) 2,502 probes had a left-out sibling in a full top 6, and
        # widening those probes found no additional pair at the threshold.
        siblings = [_ro(f"s{i}") for i in range(7)]
        writable = _rw("w", source="/w.md")  # highest id: outside a 7-chunk pool
        sims = {(a.content, b.content): 0.99 for a in siblings for b in siblings if a is not b}
        sims.update(_both_ways("s0", "w", 0.95))
        scanner, _ = scanner_factory([*siblings, writable], sims)

        candidates, coverage = await scanner.scan_with_coverage(max_scan=7)

        assert coverage.pool == 7
        assert candidates == []
        assert coverage.same_source_read_only > 0


class TestMcpScanCoverage:
    """``mem_dedup_scan`` says what it covered on both branches."""

    @staticmethod
    def _run(monkeypatch, candidates, coverage):
        from memtomem.search.dedup import DedupCoverage
        from memtomem.server.tools import dedup_decay

        app = MagicMock()
        # A bare mock attribute is truthy; the brief has its own tests.
        app.config.maintenance.experimental_duplicate_brief = False
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
            {
                "pool": 5,
                "eligible": 5,
                "near_search_enabled": True,
                "probed": 3,
                "without_vector": 2,
            },
        )

        out = await tool.mem_dedup_scan(ctx=None)

        assert out == (
            "No duplicate chunks found (threshold=0.92).\n"
            "Scanned all 5 scannable chunks; near-duplicate search probed 3, "
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
            {
                "pool": 2,
                "eligible": 2,
                "near_search_enabled": True,
                "probed": 2,
                "without_vector": 0,
            },
        )

        out = await tool.mem_dedup_scan(ctx=None)

        assert out.splitlines()[0] == "Duplicate candidates: 1 pairs (threshold=0.92):"
        assert out.splitlines()[-1] == (
            "Scanned all 2 scannable chunks; near-duplicate search probed 2."
        )

    @pytest.mark.asyncio
    async def test_bm25_only_store_says_near_search_is_off(self, monkeypatch):
        tool, _ = self._run(
            monkeypatch,
            [],
            {
                "pool": 4,
                "eligible": 4,
                "near_search_enabled": False,
                "probed": 0,
                "without_vector": 0,
            },
        )

        out = await tool.mem_dedup_scan(ctx=None)

        assert out.splitlines()[-1] == (
            "Scanned all 4 scannable chunks; near-duplicate search is off (BM25-only store)."
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("coverage", "line"),
        [
            pytest.param(
                {
                    "pool": 500,
                    "eligible": 39458,
                    "probed": 500,
                    "without_vector": 0,
                    "remaining": 38958,
                    "next_after": UUID(int=500),
                },
                "Scanned 500 of 39458 scannable chunks (the first 500 in chunk-id order); "
                "near-duplicate search probed 500; 38958 remain: "
                'continue with after="00000000-0000-0000-0000-0000000001f4".',
                id="bounded",
            ),
            pytest.param(
                {
                    "pool": 2,
                    "eligible": 3,
                    "probed": 1,
                    "without_vector": 1,
                    "remaining": 1,
                    "next_after": UUID(int=2),
                },
                "Scanned 2 of 3 scannable chunks (the first 2 in chunk-id order); "
                "near-duplicate search probed 1, "
                "1 had no usable stored vector and were not probed; 1 remains: "
                'continue with after="00000000-0000-0000-0000-000000000002".',
                id="bounded-without-vector-one-remains",
            ),
            pytest.param(
                {
                    "pool": 2,
                    "eligible": 3,
                    "probed": 0,
                    "without_vector": 0,
                    "near": False,
                    "remaining": 1,
                    "next_after": UUID(int=2),
                },
                "Scanned 2 of 3 scannable chunks (the first 2 in chunk-id order); "
                "near-duplicate search is off (BM25-only store); 1 remains: "
                'continue with after="00000000-0000-0000-0000-000000000002".',
                id="bounded-bm25-only",
            ),
            pytest.param(
                {
                    "pool": 500,
                    "eligible": 39458,
                    "probed": 500,
                    "without_vector": 0,
                    "after": UUID(int=500),
                    "remaining": 38458,
                    "next_after": UUID(int=1000),
                },
                "Scanned 500 of 39458 scannable chunks (the 500 after "
                "00000000-0000-0000-0000-0000000001f4 in chunk-id order); "
                "near-duplicate search probed 500; 38458 remain: "
                'continue with after="00000000-0000-0000-0000-0000000003e8".',
                id="cursor-mid-store",
            ),
            pytest.param(
                {
                    "pool": 458,
                    "eligible": 39458,
                    "probed": 458,
                    "without_vector": 0,
                    "after": UUID(int=39000),
                },
                "Scanned 458 of 39458 scannable chunks (the 458 after "
                "00000000-0000-0000-0000-000000009858 in chunk-id order); "
                "near-duplicate search probed 458; reached the end of the store.",
                id="cursor-last-slice",
            ),
            pytest.param(
                {
                    "pool": 0,
                    "eligible": 3,
                    "probed": 0,
                    "without_vector": 0,
                    "after": UUID(int=3),
                },
                "Scanned 0 of 3 scannable chunks (none after "
                "00000000-0000-0000-0000-000000000003 in chunk-id order); "
                "near-duplicate search probed 0; reached the end of the store.",
                id="cursor-past-the-end",
            ),
            pytest.param(
                {
                    "pool": 3,
                    "eligible": 3,
                    "probed": 3,
                    "without_vector": 0,
                    "after": UUID(int=0),
                },
                "Scanned 3 of 3 scannable chunks (the 3 after "
                "00000000-0000-0000-0000-000000000000 in chunk-id order); "
                "near-duplicate search probed 3; reached the end of the store.",
                id="cursor-below-every-id",
            ),
            pytest.param(
                {
                    "pool": 500,
                    "eligible": 39458,
                    "probed": 500,
                    "without_vector": 0,
                    "same_source_read_only": 2,
                    "pairs_found": 57,
                    "remaining": 38958,
                    "next_after": UUID(int=500),
                },
                "Scanned 500 of 39458 scannable chunks (the first 500 in chunk-id order); "
                "near-duplicate search probed 500; left out 2 pairs of read-only chunks "
                "from the same file (neither member can be removed); "
                "limit=50 kept 50 of 57 pairs: to see the rest, scan again from the same "
                "position with a higher limit; 38958 remain: "
                'continue with after="00000000-0000-0000-0000-0000000001f4".',
                id="limit-cut-then-continue",
            ),
            pytest.param(
                {
                    "pool": 3,
                    "eligible": 3,
                    "probed": 3,
                    "without_vector": 0,
                    "pairs_found": 51,
                },
                "Scanned all 3 scannable chunks; near-duplicate search probed 3; "
                "limit=50 kept 50 of 51 pairs: to see the rest, scan again from the same "
                "position with a higher limit.",
                id="limit-cut-whole-store",
            ),
            pytest.param(
                {
                    "pool": 3,
                    "eligible": 3,
                    "probed": 3,
                    "without_vector": 0,
                    "pairs_found": 500,
                },
                "Scanned all 3 scannable chunks; near-duplicate search probed 3; "
                "limit=50 kept 50 of 500 pairs: to see the rest, scan again from the same "
                "position with a higher limit.",
                id="limit-cut-the-largest-limit-lists-them-all",
            ),
            pytest.param(
                {
                    "pool": 3,
                    "eligible": 3,
                    "probed": 3,
                    "without_vector": 0,
                    "pairs_found": 501,
                },
                "Scanned all 3 scannable chunks; near-duplicate search probed 3; "
                "limit=50 kept 50 of 501 pairs: one call lists at most 500.",
                id="limit-cut-more-pairs-than-any-limit",
            ),
            pytest.param(
                {
                    "pool": 3,
                    "eligible": 3,
                    "probed": 3,
                    "without_vector": 0,
                    "pairs_found": 50,
                },
                "Scanned all 3 scannable chunks; near-duplicate search probed 3.",
                id="pairs-found-equals-the-limit",
            ),
            pytest.param(
                {"pool": 3, "eligible": 3, "probed": 3, "without_vector": 0},
                "Scanned all 3 scannable chunks; near-duplicate search probed 3.",
                id="max-scan-equals-the-count",
            ),
            pytest.param(
                {"pool": 1, "eligible": 1, "probed": 1, "without_vector": 0},
                "Scanned all 1 scannable chunk; near-duplicate search probed 1.",
                id="one-chunk",
            ),
            pytest.param(
                {"pool": 0, "eligible": 0, "probed": 0, "without_vector": 0},
                "Scanned all 0 scannable chunks; near-duplicate search probed 0.",
                id="nothing-scannable",
            ),
            pytest.param(
                {
                    "pool": 4,
                    "eligible": 4,
                    "probed": 3,
                    "without_vector": 1,
                    "same_source_read_only": 2,
                },
                "Scanned all 4 scannable chunks; near-duplicate search probed 3, "
                "1 had no usable stored vector and were not probed; left out 2 pairs of "
                "read-only chunks from the same file (neither member can be removed).",
                id="left-out-pairs",
            ),
            pytest.param(
                {
                    "pool": 2,
                    "eligible": 2,
                    "probed": 0,
                    "without_vector": 0,
                    "near": False,
                    "same_source_read_only": 1,
                },
                "Scanned all 2 scannable chunks; near-duplicate search is off (BM25-only "
                "store); left out 1 pair of read-only chunks from the same file "
                "(neither member can be removed).",
                id="left-out-pair-bm25-only",
            ),
        ],
    )
    async def test_coverage_line_says_how_much_was_scanned_and_how_it_was_chosen(
        self, monkeypatch, coverage, line
    ):
        coverage = dict(coverage)
        coverage["near_search_enabled"] = coverage.pop("near", True)
        tool, _ = self._run(monkeypatch, [], coverage)

        out = await tool.mem_dedup_scan(ctx=None)

        assert out.splitlines()[-1] == line

    @pytest.mark.asyncio
    async def test_non_positive_max_scan_is_an_error_before_scanning(self, monkeypatch):
        tool, app = self._run(
            monkeypatch,
            [],
            {
                "pool": 0,
                "eligible": 0,
                "near_search_enabled": True,
                "probed": 0,
                "without_vector": 0,
            },
        )

        out = await tool.mem_dedup_scan(max_scan=0, ctx=None)

        assert out == "Error: max_scan must be at least 1, got 0."
        app.dedup_scanner.scan_with_coverage.assert_not_awaited()

    _WHOLE = {
        "pool": 3,
        "eligible": 3,
        "near_search_enabled": True,
        "probed": 3,
        "without_vector": 0,
    }

    @pytest.mark.asyncio
    async def test_at_the_largest_limit_the_cut_names_no_remedy(self, monkeypatch):
        # 501 pairs cannot be asked for: the tool refuses a limit above 500.
        # The line says so and stops; a smaller pool would report other pairs.
        tool, _ = self._run(monkeypatch, [], {**self._WHOLE, "pairs_found": 501})

        out = await tool.mem_dedup_scan(limit=500, ctx=None)

        assert out.splitlines()[-1] == (
            "Scanned all 3 scannable chunks; near-duplicate search probed 3; "
            "limit=500 kept 500 of 501 pairs: one call lists at most 500."
        )
        assert await tool.mem_dedup_scan(limit=501, ctx=None) == (
            "Error: limit must be between 1 and 500, got 501."
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "typed",
        [
            "00000000-0000-0000-0000-0000000001f4",
            "00000000-0000-0000-0000-0000000001F4",
            "{00000000-0000-0000-0000-0000000001f4}",
            "000000000000000000000000000001f4",
            "urn:uuid:00000000-0000-0000-0000-0000000001f4",
        ],
        ids=["as-printed", "uppercase", "braces", "no-hyphens", "urn"],
    )
    async def test_after_reaches_the_scanner_as_the_id_it_names(self, monkeypatch, typed):
        tool, app = self._run(monkeypatch, [], self._WHOLE)

        await tool.mem_dedup_scan(after=typed, ctx=None)

        app.dedup_scanner.scan_with_coverage.assert_awaited_once_with(
            threshold=0.92, limit=50, max_scan=500, after=UUID(int=500)
        )

    @pytest.mark.asyncio
    async def test_no_after_scans_from_the_start(self, monkeypatch):
        tool, app = self._run(monkeypatch, [], self._WHOLE)

        await tool.mem_dedup_scan(ctx=None)

        app.dedup_scanner.scan_with_coverage.assert_awaited_once_with(
            threshold=0.92, limit=50, max_scan=500, after=None
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("typed", "shown"),
        [
            ("not-an-id", "'not-an-id'"),
            ("", "''"),
            ("00000000-0000-0000-0000-0000000001f", "'00000000-0000-0000-0000-0000000001f'"),
            (" 00000000-0000-0000-0000-0000000001f4", "' 00000000-0000-0000-0000-0000000001f4'"),
            (500, "500"),
            (["00000000-0000-0000-0000-0000000001f4"], "['00000000-0000-0000-0000-0000000001f4']"),
        ],
        ids=["words", "empty", "one-digit-short", "leading-space", "int", "list"],
    )
    async def test_an_after_that_is_not_a_chunk_id_is_an_error_before_scanning(
        self, monkeypatch, typed, shown
    ):
        tool, app = self._run(monkeypatch, [], self._WHOLE)

        out = await tool.mem_dedup_scan(after=typed, ctx=None)

        assert out == f"Error: after must be a chunk id, got {shown}."
        app.dedup_scanner.scan_with_coverage.assert_not_awaited()


class TestParseScanCursor:
    def test_none_is_no_cursor(self):
        assert parse_scan_cursor(None) is None

    def test_a_uuid_object_is_not_a_typed_cursor(self):
        # The surfaces hand over what a caller typed, which is text.
        with pytest.raises(ValueError, match="after must be a chunk id"):
            parse_scan_cursor(UUID(int=1))


class TestCursorAndLimit:
    """A scan continued with ``after``, and what ``limit`` cut from one pool."""

    @pytest.mark.asyncio
    async def test_the_cursor_reaches_storage_as_stored_id_text(self, scanner_factory):
        chunks = [_mk(f"c{i}", id=UUID(int=n)) for i, n in enumerate((0xA, 0xB, 0xC))]
        scanner, storage = scanner_factory(chunks)

        _, first = await scanner.scan_with_coverage(max_scan=2)
        _, second = await scanner.scan_with_coverage(max_scan=2, after=first.next_after)

        assert storage.cursors == [None, "00000000-0000-0000-0000-00000000000b"]
        assert (first.after, first.pool, first.remaining) == (None, 2, 1)
        assert (second.after, second.pool, second.remaining) == (UUID(int=0xB), 1, 0)
        assert second.next_after is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("limit", "returned"),
        [(1, 1), (2, 2), (3, 3), (4, 3)],
        ids=["cut-to-one", "cut-to-two", "exactly-all", "room-to-spare"],
    )
    async def test_pairs_found_is_the_count_before_the_limit(
        self, scanner_factory, limit, returned
    ):
        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        same = [_mk("one text", created_at=t0 + timedelta(minutes=i)) for i in range(4)]
        scanner, _ = scanner_factory(same)

        candidates, coverage = await scanner.scan_with_coverage(limit=limit)

        assert len(candidates) == returned
        assert coverage.pairs_found == 3

    @pytest.mark.asyncio
    async def test_left_out_pairs_are_not_pairs_found(self, scanner_factory):
        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        kept_a = _rw("note", source="/a.md", created_at=t0)
        kept_b = _rw("note", source="/b.md", created_at=t0 + timedelta(hours=1))
        scanner, _ = scanner_factory([_ro("{x}"), _ro("{x}"), kept_a, kept_b])

        candidates, coverage = await scanner.scan_with_coverage()

        assert len(candidates) == 1
        assert (coverage.pairs_found, coverage.same_source_read_only) == (1, 1)

    @pytest.mark.asyncio
    async def test_a_smaller_pool_reports_other_pairs_than_the_ones_limit_cut(
        self, scanner_factory
    ):
        # Why the coverage line names no smaller ``max_scan``: the exact phase
        # pairs each copy with the oldest copy in its pool. 502 chunks with one
        # body are 501 pairs with the first chunk, one more than a caller can
        # ask for. Two pools of 251 are each within ``limit``, but the second
        # pool's pairs are with its own oldest chunk, and the pair ``limit``
        # cut is in neither.
        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        same = [_mk("one text", created_at=t0 + timedelta(seconds=i)) for i in range(502)]
        ids = [c.id for c in same]
        scanner, _ = scanner_factory(same, dense_enabled=False)

        def pairs(candidates):
            return {(c.chunk_a.id, c.chunk_b.id) for c in candidates}

        cut, whole = await scanner.scan_with_coverage(limit=500, max_scan=502)
        half, first = await scanner.scan_with_coverage(limit=500, max_scan=251)
        rest, second = await scanner.scan_with_coverage(
            limit=500, max_scan=251, after=first.next_after
        )

        assert whole.pairs_found == 501
        assert pairs(cut) == {(ids[0], dup) for dup in ids[1:501]}
        assert (first.pairs_found, first.remaining) == (250, 251)
        assert (second.pairs_found, second.remaining) == (250, 0)
        assert pairs(half) == {(ids[0], dup) for dup in ids[1:251]}
        assert pairs(rest) == {(ids[251], dup) for dup in ids[252:]}
        assert (ids[0], ids[501]) not in pairs(half) | pairs(rest)

    @pytest.mark.asyncio
    async def test_a_near_pair_across_two_pools_is_reported_from_both_when_each_finds_the_other(
        self, scanner_factory
    ):
        # Stated limit: neighbours are store-wide, so a member's pool finds the
        # pair when that member's search returns the other. A walk's caller
        # tells repeats apart by the two ids.
        a, b = _mk("one text"), _mk("one txt")
        scanner, _ = scanner_factory([a, b], _both_ways("one text", "one txt", 0.97))

        first, coverage = await scanner.scan_with_coverage(max_scan=1)
        second, _ = await scanner.scan_with_coverage(max_scan=1, after=coverage.next_after)

        assert [{c.chunk_a.id, c.chunk_b.id} for c in first] == [{a.id, b.id}]
        assert [{c.chunk_a.id, c.chunk_b.id} for c in second] == [{a.id, b.id}]

    @pytest.mark.asyncio
    async def test_a_near_pair_found_one_way_is_reported_from_one_pool_only(self, scanner_factory):
        # A search returns a chunk's own nearest neighbours, so it is not
        # symmetric: here the first chunk finds the second and is not found
        # by it. The repeat is common, not guaranteed.
        a, b = _mk("one text"), _mk("one txt")
        scanner, _ = scanner_factory([a, b], {("one text", "one txt"): 0.97})

        first, coverage = await scanner.scan_with_coverage(max_scan=1)
        second, _ = await scanner.scan_with_coverage(max_scan=1, after=coverage.next_after)

        assert [{c.chunk_a.id, c.chunk_b.id} for c in first] == [{a.id, b.id}]
        assert second == []

    @pytest.mark.asyncio
    async def test_an_exact_pair_across_two_pools_is_not_found_without_vectors(
        self, scanner_factory
    ):
        # Stated limit: the exact phase groups one pool. On a BM25-only store
        # nothing else looks, so a walk in steps misses what one pool finds.
        a, b = _mk("one text"), _mk("one text")
        scanner, _ = scanner_factory([a, b], dense_enabled=False)

        first, coverage = await scanner.scan_with_coverage(max_scan=1)
        second, _ = await scanner.scan_with_coverage(max_scan=1, after=coverage.next_after)
        together, _ = await scanner.scan_with_coverage(max_scan=2)

        assert (first, second) == ([], [])
        assert [(c.chunk_a.id, c.chunk_b.id, c.exact) for c in together] == [(a.id, b.id, True)]


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
        """A failed apply normally writes nothing, but the invalidation does
        not depend on where the failure happened."""
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

    @pytest.mark.asyncio
    async def test_keeper_in_delete_ids_is_an_error_string(self, monkeypatch):
        keep, dup = _mk("same"), _mk("same")
        storage = _FakeStorage([keep, dup])
        app = self._app()
        app.dedup_scanner = DedupScanner(storage)
        dedup_decay = self._patch(monkeypatch, app)

        out = await dedup_decay.mem_dedup_merge(
            keep_id=str(keep.id), delete_ids=[str(keep.id), str(dup.id)], dry_run=False, ctx=None
        )

        assert out.startswith("Error: keep_id is also listed in delete_ids")
        assert {c.id for c in storage._chunks} == {keep.id, dup.id}


class TestMergeIdNormalisation:
    """A keeper listed for deletion is refused, and repeated ids count once (#2787)."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("dry_run", [True, False])
    @pytest.mark.parametrize("with_dup", [True, False])
    async def test_keeper_in_delete_ids_is_refused_before_any_read(self, dry_run, with_dup):
        keep, dup = _mk("same"), _mk("same")
        storage = _FakeStorage([keep, dup])
        delete_ids = [keep.id, dup.id] if with_dup else [keep.id]

        with pytest.raises(ValueError, match="keep_id is also listed in delete_ids"):
            await DedupScanner(storage).merge(keep.id, delete_ids, dry_run=dry_run)

        assert {c.id for c in storage._chunks} == {keep.id, dup.id}
        assert storage.batch_reads == []
        assert storage.transactions == 0
        assert storage.deletes == []

    @pytest.mark.asyncio
    async def test_repeated_ids_count_once_in_preview_and_apply(self):
        keep, d, e = _mk("same"), _mk("same"), _mk("same")
        storage = _FakeStorage([keep, d, e])
        scanner = DedupScanner(storage)

        assert await scanner.merge(keep.id, [d.id, e.id, d.id], dry_run=True) == 2
        assert await scanner.merge(keep.id, [d.id, e.id, d.id]) == 2
        assert storage.deletes == [[d.id, e.id]]


@pytest.mark.asyncio
async def test_merge_that_would_cascade_to_the_keeper_is_rolled_back(
    components, memory_dir, caplog
):
    """Emptying a source removes its policy summary; a summary kept by the merge must survive."""
    import logging

    from memtomem.models import CONSOLIDATED_SUFFIX, ORIGIN_CONSOLIDATION_POLICY
    from memtomem.storage.sqlite_helpers import norm_path

    storage = components.storage
    parent = memory_dir / "parent.md"
    keep = Chunk(
        content="same body",
        metadata=ChunkMetadata(
            source_file=Path(norm_path(parent) + CONSOLIDATED_SUFFIX),
            origin=ORIGIN_CONSOLIDATION_POLICY,
            tags=("summary",),
        ),
        embedding=[0.1] * 1024,
    )
    last = Chunk(
        content="same body",
        metadata=ChunkMetadata(source_file=parent, tags=("from-parent",)),
        embedding=[0.1] * 1024,
    )
    await storage.upsert_chunks([keep, last])

    caplog.set_level(logging.INFO, logger="memtomem.storage.sqlite_backend")
    with pytest.raises(ValueError, match="would delete the kept chunk"):
        await DedupScanner(storage).merge(keep.id, [last.id])

    left = await storage.get_chunks_batch([keep.id, last.id])
    assert set(left) == {keep.id, last.id}
    assert left[keep.id].metadata.tags == ("summary",)
    # The removal was rolled back, so no log line may report it as done.
    messages = [r.getMessage() for r in caplog.records]
    assert any("pending the caller's commit" in m for m in messages)
    assert not any(m.startswith("Deleted") and "policy summary" in m for m in messages)
