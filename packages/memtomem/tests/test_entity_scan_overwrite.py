"""mem_entity_scan write contract — stale rows and search-cache coherence.

Entities became a ranking input with the Stage-7b boost, so a scan that leaves
old rows behind (or leaves the search cache holding pre-scan results) is a
ranking bug, not just bookkeeping.
"""

from unittest.mock import patch

import pytest
from helpers import StubCtx, make_chunk

from memtomem.server.context import AppContext
from memtomem.server.tools.entity import mem_entity_scan


async def _no_entities(*_args, **_kwargs):
    """Stand in for an extractor pass that now finds nothing."""
    return []


async def _seed(comp, content="sqlite migration owned by @alice"):
    """Index one chunk with one entity, via a real scan."""
    chunk = make_chunk(content, source="note.md")
    await comp.storage.upsert_chunks([chunk])
    await comp.storage.upsert_entities(
        str(chunk.id),
        [{"entity_type": "technology", "entity_value": "sqlite", "confidence": 0.9}],
    )
    return chunk


class TestOverwriteClearsStaleRows:
    @pytest.mark.asyncio
    async def test_overwrite_with_no_entities_deletes_old_rows(self, bm25_only_components):
        comp, _ = bm25_only_components
        chunk = await _seed(comp)
        assert await comp.storage.get_entities_for_chunk(str(chunk.id))

        ctx = StubCtx(AppContext.from_components(comp))
        with patch("memtomem.tools.entity_extraction.extract_entities_with_llm", _no_entities):
            out = await mem_entity_scan(overwrite=True, ctx=ctx)  # type: ignore[arg-type]

        # The chunk's content no longer yields entities, so the old rows must be
        # gone — otherwise they keep boosting it for a query it no longer matches.
        assert await comp.storage.get_entities_for_chunk(str(chunk.id)) == []
        assert "Chunks cleared" in out

    @pytest.mark.asyncio
    async def test_dry_run_reports_but_does_not_delete(self, bm25_only_components):
        comp, _ = bm25_only_components
        chunk = await _seed(comp)

        ctx = StubCtx(AppContext.from_components(comp))
        with patch("memtomem.tools.entity_extraction.extract_entities_with_llm", _no_entities):
            out = await mem_entity_scan(overwrite=True, dry_run=True, ctx=ctx)  # type: ignore[arg-type]

        assert await comp.storage.get_entities_for_chunk(str(chunk.id))  # untouched
        assert "dry run" in out

    @pytest.mark.asyncio
    async def test_non_overwrite_leaves_scanned_chunks_alone(self, bm25_only_components):
        # Without overwrite an already-scanned chunk is skipped entirely; the
        # clear path must not fire for it.
        comp, _ = bm25_only_components
        chunk = await _seed(comp)

        ctx = StubCtx(AppContext.from_components(comp))
        with patch("memtomem.tools.entity_extraction.extract_entities_with_llm", _no_entities):
            await mem_entity_scan(overwrite=False, ctx=ctx)  # type: ignore[arg-type]

        assert await comp.storage.get_entities_for_chunk(str(chunk.id))


class TestScanInvalidatesSearchCache:
    @pytest.mark.asyncio
    async def test_write_invalidates(self, bm25_only_components):
        comp, _ = bm25_only_components
        chunk = make_chunk("sqlite migration notes")
        await comp.storage.upsert_chunks([chunk])

        ctx = StubCtx(AppContext.from_components(comp))
        before = comp.search_pipeline._cache_version
        await mem_entity_scan(ctx=ctx)  # type: ignore[arg-type]
        assert comp.search_pipeline._cache_version > before

    @pytest.mark.asyncio
    async def test_dry_run_does_not_invalidate(self, bm25_only_components):
        comp, _ = bm25_only_components
        await comp.storage.upsert_chunks([make_chunk("sqlite migration notes")])

        ctx = StubCtx(AppContext.from_components(comp))
        before = comp.search_pipeline._cache_version
        await mem_entity_scan(dry_run=True, ctx=ctx)  # type: ignore[arg-type]
        assert comp.search_pipeline._cache_version == before

    @pytest.mark.asyncio
    async def test_failure_after_a_write_still_invalidates(self, bm25_only_components):
        """Each upsert commits on its own, so a later failure must not leave the
        cache serving results computed from the pre-scan rows."""
        comp, _ = bm25_only_components
        for i in range(3):
            chunk = make_chunk(f"sqlite note {i}", source=f"n{i}.md")
            await comp.storage.upsert_chunks([chunk])

        calls = {"n": 0}

        async def _boom(text, entity_types, provider):
            from memtomem.tools.entity_extraction import ExtractedEntity

            calls["n"] += 1
            if calls["n"] > 1:
                raise RuntimeError("extractor died")
            return [ExtractedEntity("technology", "sqlite", 0.9, 0)]

        ctx = StubCtx(AppContext.from_components(comp))
        before = comp.search_pipeline._cache_version
        with patch("memtomem.tools.entity_extraction.extract_entities_with_llm", _boom):
            # ``tool_handler`` converts the raised error into an error string;
            # the point is that the first (already committed) write is not left
            # behind a stale cache.
            out = await mem_entity_scan(ctx=ctx)  # type: ignore[arg-type]

        assert calls["n"] > 1
        assert "extractor died" in out.lower() or "error" in out.lower()
        assert comp.search_pipeline._cache_version > before
