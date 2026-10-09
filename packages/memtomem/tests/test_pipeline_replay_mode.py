"""``record=False`` on ``SearchPipeline.search`` (#1802).

A ``record=False`` search must be a no-side-effects read: no access-counter
mutation, no query-run observation, and neither served from nor stored into
the TTL result cache or the LLM-expansion cache. It must also pin time decay
to the supplied ``as_of_unix`` so a delayed replay is byte-stable. Exhaustive
dense selection is a separate switch (#2671) — see
``test_dense_exhaustive_replay.py``.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from helpers import make_chunk as _make_chunk


async def _drain_bg(pipeline):
    if pipeline._bg_tasks:
        await asyncio.gather(*list(pipeline._bg_tasks), return_exceptions=True)


class TestNoSideEffects:
    @pytest.mark.asyncio
    async def test_record_false_touches_no_state(self, bm25_only_components):
        comp, _ = bm25_only_components
        storage, pipeline = comp.storage, comp.search_pipeline

        chunks = [_make_chunk("replay marker body", source=f"n{i}.md") for i in range(4)]
        await storage.upsert_chunks(chunks)
        ids = [c.id for c in chunks]

        before = await storage.get_access_counts(ids)

        results, stats = await pipeline.search("replay", record=False)
        await _drain_bg(pipeline)

        assert results, "sanity: BM25 should find the seeded chunks"
        assert stats.query_run_id is None
        # No access-counter mutation.
        assert await storage.get_access_counts(ids) == before
        # No observation persisted.
        assert await storage.get_search_runs() == []
        # Neither cache populated.
        assert pipeline._search_cache == {}
        assert pipeline._expansion_cache == {}

    @pytest.mark.asyncio
    async def test_record_true_still_has_side_effects(self, bm25_only_components):
        comp, _ = bm25_only_components
        storage, pipeline = comp.storage, comp.search_pipeline

        chunks = [_make_chunk("record marker body", source=f"r{i}.md") for i in range(4)]
        await storage.upsert_chunks(chunks)
        ids = [c.id for c in chunks]

        results, stats = await pipeline.search("record", record=True)
        await _drain_bg(pipeline)

        assert results
        assert stats.query_run_id is not None
        counts = await storage.get_access_counts(ids)
        assert all(counts.get(str(cid), 0) >= 1 for cid in ids)
        assert len(await storage.get_search_runs()) == 1
        assert pipeline._search_cache, "record=True must populate the TTL cache"


class TestCacheIsolation:
    @pytest.mark.asyncio
    async def test_replay_not_served_from_and_preserves_cache(self, bm25_only_components):
        comp, _ = bm25_only_components
        storage, pipeline = comp.storage, comp.search_pipeline

        chunks = [_make_chunk("cache marker body", source=f"c{i}.md") for i in range(4)]
        await storage.upsert_chunks(chunks)

        # Warm the cache with a normal search.
        await pipeline.search("cache", record=True)
        await _drain_bg(pipeline)
        assert pipeline._search_cache
        cache_snapshot = dict(pipeline._search_cache)

        # An identical replay within TTL must not be served from the cache
        # (cache_hit stays False) and must not evict/replace the entry.
        _, stats = await pipeline.search("cache", record=False)
        await _drain_bg(pipeline)
        assert stats.cache_hit is False
        assert pipeline._search_cache == cache_snapshot

    @pytest.mark.asyncio
    async def test_a_visibility_change_still_invalidates_under_record_false(
        self, bm25_only_components, monkeypatch
    ):
        """Bypassing the cache is not freezing it.

        A ``record=False`` call that observes a new source-visibility epoch
        drops the cached entries like any other search would: they describe
        rows whose visibility has changed, whoever notices first.
        """
        comp, _ = bm25_only_components
        storage, pipeline = comp.storage, comp.search_pipeline

        await storage.upsert_chunks([_make_chunk("epoch marker body", source="e.md")])
        await pipeline.search("epoch", record=True)
        await _drain_bg(pipeline)
        assert pipeline._search_cache
        epoch = await storage.source_visibility_epoch()

        async def _next_epoch() -> int:
            return epoch + 1

        monkeypatch.setattr(storage, "source_visibility_epoch", _next_epoch)

        await pipeline.search("epoch", record=False)
        await _drain_bg(pipeline)
        assert pipeline._search_cache == {}


class TestDecayPinning:
    @pytest.mark.asyncio
    async def test_decay_pinned_to_as_of(self, bm25_only_components, monkeypatch):
        comp, _ = bm25_only_components
        storage, pipeline = comp.storage, comp.search_pipeline
        monkeypatch.setattr(pipeline._decay_config, "enabled", True)

        await storage.upsert_chunks([_make_chunk("decay marker body", source="d.md")])

        as_of = int(time.time())
        r1, _ = await pipeline.search("decay", as_of_unix=as_of, record=False)
        # A second replay pinned to the SAME instant scores identically, even
        # though wall-clock has advanced between the two calls.
        r2, _ = await pipeline.search("decay", as_of_unix=as_of, record=False)
        assert [round(r.score, 9) for r in r1] == [round(r.score, 9) for r in r2]

        # A later ``as_of`` decays the same chunk further — proving decay is
        # driven by the pinned instant, not ignored.
        r3, _ = await pipeline.search("decay", as_of_unix=as_of + 400 * 86400, record=False)
        assert r3
        assert r3[0].score < r1[0].score
