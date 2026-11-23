"""mem_entity_scan write contract — stale rows and search-cache coherence.

Entities became a ranking input with the Stage-7b boost, so a scan that leaves
old rows behind (or leaves the search cache holding pre-scan results) is a
ranking bug, not just bookkeeping.
"""

from pathlib import Path
from unittest.mock import patch

import pytest
from helpers import StubCtx, make_chunk

from memtomem.server.context import AppContext
from memtomem.server.tools.entity import mem_entity_scan


async def _no_entities(*_args, **_kwargs):
    """Stand in for an extractor pass that now finds nothing."""
    from memtomem.tools.entity_extraction import EntityExtraction

    return EntityExtraction([], llm_failed=False)


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


class TestScanCoversWholeSource:
    """The scan must reach every chunk of a source, however large.

    ``list_chunks_by_source`` defaults to ``limit=50``, and the scan used to
    call it bare — silently covering only the first 50 chunks of each source.
    ``overwrite=False`` re-runs skip already-extracted chunks, so the tail was
    not merely late: it was unreachable forever.
    """

    @pytest.mark.asyncio
    async def test_large_source_is_fully_scanned(self, bm25_only_components):
        comp, _ = bm25_only_components
        # Far past both the old silent cap (50) and any plausible default.
        n = 501
        chunks = [make_chunk(f"note {i}", source="big.md") for i in range(n)]
        await comp.storage.upsert_chunks(chunks)

        # Storage-level pin first: ``limit=None`` reads the whole source in one
        # snapshot — no cap, no duplicates, no gaps.
        unbounded = await comp.storage.list_chunks_by_source(Path("/tmp/big.md"), limit=None)
        assert {str(c.id) for c in unbounded} == {str(c.id) for c in chunks}
        assert len(unbounded) == n

        scanned: set[str] = set()

        async def _one_entity(text, entity_types, provider):
            from memtomem.tools.entity_extraction import EntityExtraction, ExtractedEntity

            scanned.add(text)
            return EntityExtraction([ExtractedEntity("technology", "sqlite", 0.9, 0)], False)

        ctx = StubCtx(AppContext.from_components(comp))
        with patch("memtomem.tools.entity_extraction.extract_entities_with_llm", _one_entity):
            out = await mem_entity_scan(ctx=ctx)  # type: ignore[arg-type]

        assert len(scanned) == n
        assert f"Chunks with entities: {n}" in out
        for chunk in (chunks[0], chunks[49], chunks[50], chunks[-1]):
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
            from memtomem.tools.entity_extraction import EntityExtraction, ExtractedEntity

            calls["n"] += 1
            if calls["n"] > 1:
                raise RuntimeError("extractor died")
            return EntityExtraction([ExtractedEntity("technology", "sqlite", 0.9, 0)], False)

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


class TestEntityTypeValidation:
    """An unknown type must never reach the clear path.

    The extractor ignores types it does not know, so a typo yields zero
    entities for every chunk — which under ``overwrite`` is indistinguishable
    from "this content has no entities any more" and would delete the lot.
    """

    @pytest.mark.asyncio
    async def test_typo_is_rejected_before_any_write(self, bm25_only_components):
        comp, _ = bm25_only_components
        chunk = await _seed(comp)

        ctx = StubCtx(AppContext.from_components(comp))
        before = comp.search_pipeline._cache_version
        out = await mem_entity_scan(  # type: ignore[arg-type]
            entity_types=["technlogy"], overwrite=True, ctx=ctx
        )

        assert "unknown entity type" in out.lower()
        assert await comp.storage.get_entities_for_chunk(str(chunk.id))  # nothing deleted
        assert comp.search_pipeline._cache_version == before  # nothing invalidated

    @pytest.mark.asyncio
    async def test_error_names_the_valid_vocabulary(self, bm25_only_components):
        comp, _ = bm25_only_components
        ctx = StubCtx(AppContext.from_components(comp))
        out = await mem_entity_scan(entity_types=["nope"], ctx=ctx)  # type: ignore[arg-type]
        for valid in ("person", "date", "technology", "concept"):
            assert valid in out

    @pytest.mark.asyncio
    async def test_one_bad_type_among_good_ones_still_rejects(self, bm25_only_components):
        comp, _ = bm25_only_components
        chunk = await _seed(comp)
        ctx = StubCtx(AppContext.from_components(comp))
        out = await mem_entity_scan(  # type: ignore[arg-type]
            entity_types=["person", "technlogy"], overwrite=True, ctx=ctx
        )
        assert "technlogy" in out
        assert await comp.storage.get_entities_for_chunk(str(chunk.id))

    @pytest.mark.asyncio
    async def test_valid_types_still_run(self, bm25_only_components):
        comp, _ = bm25_only_components
        await comp.storage.upsert_chunks([make_chunk("sqlite migration notes")])
        ctx = StubCtx(AppContext.from_components(comp))
        out = await mem_entity_scan(entity_types=["technology"], ctx=ctx)  # type: ignore[arg-type]
        assert "unknown entity type" not in out.lower()
        assert "complete" in out.lower()


class _RaisingLLM:
    """A provider whose every call fails, the way an unreachable Ollama does."""

    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, prompt: str, *, system: str = "", max_tokens: int = 1024) -> str:
        from memtomem.errors import LLMError

        self.calls += 1
        raise LLMError("Cannot connect to Ollama")

    async def close(self) -> None:
        pass


class _AnsweringLLM:
    """A provider that answers every call with a fixed reply."""

    def __init__(self, reply: str) -> None:
        self._reply = reply

    async def generate(self, prompt: str, *, system: str = "", max_tokens: int = 1024) -> str:
        return self._reply

    async def close(self) -> None:
        pass


async def _seed_person(comp, content: str):
    """One chunk holding a row only the LLM would find (``person|Alice``)."""
    chunk = make_chunk(content, source="note.md")
    await comp.storage.upsert_chunks([chunk])
    await comp.storage.upsert_entities(
        str(chunk.id),
        [{"entity_type": "person", "entity_value": "Alice", "confidence": 0.95}],
    )
    return chunk


def _rows(entities: list[dict]) -> set[tuple[str, str]]:
    return {(e["entity_type"], e["entity_value"]) for e in entities}


async def _scan(comp, **kwargs) -> str:
    return await mem_entity_scan(ctx=StubCtx(AppContext.from_components(comp)), **kwargs)  # type: ignore[arg-type]


class TestLlmFailureLeavesChunksUnchanged:
    """A failed LLM call is not an extraction (#3383).

    The wrapper falls back to the regex extractor when the LLM call fails. The
    scan used to take that result as the chunk's new entities, so an
    ``overwrite`` scan during an outage replaced LLM rows with regex rows, or
    cleared them when the regex found nothing. These drive the real wrapper with
    a failing provider; nothing here patches the extractor.
    """

    @pytest.mark.asyncio
    async def test_overwrite_does_not_clear_when_regex_finds_nothing(self, bm25_only_components):
        from memtomem.tools.entity_extraction import extract_entities

        comp, _ = bm25_only_components
        chunk = await _seed_person(comp, "Alice joined")
        assert extract_entities("Alice joined") == []  # the case that used to clear
        comp.llm = _RaisingLLM()

        out = await _scan(comp, overwrite=True)

        assert comp.llm.calls == 1
        assert _rows(await comp.storage.get_entities_for_chunk(str(chunk.id))) == {
            ("person", "Alice")
        }
        assert "Chunks cleared" not in out
        assert "- Chunks left unchanged (LLM extraction failed): 1" in out.splitlines()
        assert "- Chunks with entities: 0" in out.splitlines()

    @pytest.mark.asyncio
    async def test_overwrite_does_not_replace_with_regex_rows(self, bm25_only_components):
        from memtomem.tools.entity_extraction import extract_entities

        comp, _ = bm25_only_components
        content = "Alice chose SQLite for the cache"
        chunk = await _seed_person(comp, content)
        assert extract_entities(content)  # the case that used to replace
        comp.llm = _RaisingLLM()

        out = await _scan(comp, overwrite=True)

        assert _rows(await comp.storage.get_entities_for_chunk(str(chunk.id))) == {
            ("person", "Alice")
        }
        assert "- Chunks left unchanged (LLM extraction failed): 1" in out.splitlines()
        assert "- Total entities found: 0" in out.splitlines()

    @pytest.mark.asyncio
    async def test_a_chunk_without_rows_gets_no_regex_rows(self, bm25_only_components):
        # Plain scan, regex-positive content, no rows yet (for example an
        # earlier overwrite scan's NONE cleared them): the failed call writes
        # nothing, and the chunk is reported.
        from memtomem.tools.entity_extraction import extract_entities

        comp, _ = bm25_only_components
        content = "Alice chose SQLite for the cache"
        chunk = make_chunk(content, source="note.md")
        await comp.storage.upsert_chunks([chunk])
        assert extract_entities(content)
        comp.llm = _RaisingLLM()

        out = await _scan(comp)

        assert await comp.storage.get_entities_for_chunk(str(chunk.id)) == []
        assert "- Chunks left unchanged (LLM extraction failed): 1" in out.splitlines()

    @pytest.mark.asyncio
    async def test_none_answer_still_clears(self, bm25_only_components):
        # NONE is the model saying the content has no entities, not a failure.
        comp, _ = bm25_only_components
        chunk = await _seed_person(comp, "Alice joined")
        comp.llm = _AnsweringLLM("NONE")

        out = await _scan(comp, overwrite=True)

        assert await comp.storage.get_entities_for_chunk(str(chunk.id)) == []
        assert "- Chunks cleared (no entities on re-scan): 1" in out.splitlines()
        assert "LLM extraction failed" not in out

    @pytest.mark.asyncio
    async def test_an_unparseable_reply_is_a_failure(self, bm25_only_components):
        comp, _ = bm25_only_components
        chunk = await _seed_person(comp, "Alice joined")
        comp.llm = _AnsweringLLM("I cannot extract entities from this text")

        out = await _scan(comp, overwrite=True)

        assert _rows(await comp.storage.get_entities_for_chunk(str(chunk.id))) == {
            ("person", "Alice")
        }
        assert "Chunks cleared" not in out
        assert "- Chunks left unchanged (LLM extraction failed): 1" in out.splitlines()

    @pytest.mark.asyncio
    async def test_dry_run_counts_the_failure(self, bm25_only_components):
        comp, _ = bm25_only_components
        chunk = await _seed_person(comp, "Alice joined")
        comp.llm = _RaisingLLM()

        out = await _scan(comp, overwrite=True, dry_run=True)

        assert _rows(await comp.storage.get_entities_for_chunk(str(chunk.id))) == {
            ("person", "Alice")
        }
        lines = out.splitlines()
        assert "- Chunks left unchanged (LLM extraction failed): 1 (dry run)" in lines
        # The preview must not promise the clear the live scan would not do.
        assert "Chunks cleared" not in out

    @pytest.mark.asyncio
    async def test_a_scan_that_only_failed_does_not_invalidate(self, bm25_only_components):
        comp, _ = bm25_only_components
        await _seed_person(comp, "Alice chose SQLite for the cache")
        comp.llm = _RaisingLLM()
        before = comp.search_pipeline._cache_version

        out = await _scan(comp, overwrite=True)

        # The scan must have run to the end and reached the provider: an error
        # string from ``tool_handler`` would leave the version unchanged too.
        assert out.startswith("Entity scan complete"), out
        assert comp.llm.calls == 1
        assert "- Chunks left unchanged (LLM extraction failed): 1" in out.splitlines()
        assert comp.search_pipeline._cache_version == before
