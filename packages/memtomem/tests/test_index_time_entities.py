"""Entities are written with the chunk, on the indexer's write path (#2145).

Before this, ``chunk_entities`` was populated only by ``mem_entity_scan``, so a
default install had none at all and coverage shrank from there: re-indexing an
edited file rewrote the chunk and left its entities describing content that was
gone. These tests pin the write, the rewrite, and the clear — the rewrite being
the one that used to be a silent decay.
"""

from __future__ import annotations

import pytest

# Content whose regex extraction is stable: one technology term, one person,
# one date. Kept as constants so a test asserting absence names the same value
# the seeding test asserted presence of.
_WITH_PYTHON = "We decided to use Python for the parser.\n"
_WITH_RUST = "sqlite migration owned by Bob Lee on 2026-01-02\n"
_NO_ENTITIES = "aaa bbb ccc\n"


async def _entity_values(comp) -> set[tuple[str, str]]:
    """Every stored ``(entity_type, entity_value)``, across all chunks."""
    db = comp.storage._get_db()
    return {
        (row[0], row[1])
        for row in db.execute("SELECT entity_type, entity_value FROM chunk_entities").fetchall()
    }


async def _index(comp, path) -> None:
    await comp.index_engine.index_file(path)


class TestIndexTimeExtraction:
    @pytest.mark.asyncio
    async def test_indexing_populates_entities(self, bm25_only_components):
        comp, mem_dir = bm25_only_components
        note = mem_dir / "note.md"
        note.write_text(_WITH_PYTHON, encoding="utf-8")

        await _index(comp, note)

        assert ("technology", "Python") in await _entity_values(comp)

    @pytest.mark.asyncio
    async def test_reindex_after_edit_rewrites_entities(self, bm25_only_components):
        """The decay this issue is about: the old rows must go, the new arrive.

        Asserting only that the new entity appears would pass against the old
        behaviour on a delete-and-reinsert diff; the negative half is the pin.
        """
        comp, mem_dir = bm25_only_components
        note = mem_dir / "note.md"
        note.write_text(_WITH_PYTHON, encoding="utf-8")
        await _index(comp, note)
        assert ("technology", "Python") in await _entity_values(comp)

        note.write_text(_WITH_RUST, encoding="utf-8")
        await _index(comp, note)

        values = await _entity_values(comp)
        assert ("technology", "Python") not in values, "stale entity survived a re-index"
        assert ("person", "Bob Lee") in values
        assert ("date", "2026-01-02") in values

    @pytest.mark.asyncio
    async def test_reindex_to_entityless_content_clears_rows(self, bm25_only_components):
        """``upsert_entities`` early-returns on an empty list, so the indexer
        has to delete explicitly — otherwise the chunk keeps boosting for a
        query its content no longer matches."""
        comp, mem_dir = bm25_only_components
        note = mem_dir / "note.md"
        note.write_text(_WITH_PYTHON, encoding="utf-8")
        await _index(comp, note)
        assert await _entity_values(comp)

        note.write_text(_NO_ENTITIES, encoding="utf-8")
        await _index(comp, note)

        assert await _entity_values(comp) == set()

    @pytest.mark.asyncio
    async def test_disabled_knob_writes_nothing(self, bm25_only_components):
        comp, mem_dir = bm25_only_components
        comp.config.indexing.extract_entities = False
        note = mem_dir / "note.md"
        note.write_text(_WITH_PYTHON, encoding="utf-8")

        await _index(comp, note)

        assert await _entity_values(comp) == set()

    @pytest.mark.asyncio
    async def test_entities_are_committed_with_the_chunk(self, bm25_only_components):
        """Extraction runs inside the chunk-write transaction, so a chunk that
        is visible must have its entities visible too — never a chunk row whose
        entities are still pending."""
        comp, mem_dir = bm25_only_components
        note = mem_dir / "note.md"
        note.write_text(_WITH_RUST, encoding="utf-8")

        await _index(comp, note)

        chunks = await comp.storage.list_chunks_by_source(note)
        assert chunks
        for chunk in chunks:
            rows = await comp.storage.get_entities_for_chunk(str(chunk.id))
            assert rows, f"chunk {chunk.id} committed without its entities"


class TestRaceLoserChunk:
    @pytest.mark.asyncio
    async def test_dropped_chunk_does_not_break_the_write(self, bm25_only_components):
        """A chunk that loses the #691 uniqueness race has no row to hang
        entities off, and the foreign key is enforced — so extracting for it
        would roll back the chunk write that *did* succeed.

        Simulated by pre-inserting the winner: a second chunk with the same
        ``(namespace, source_file, content_hash, start_line)`` under a
        different id is exactly what ``INSERT OR IGNORE`` drops.
        """
        comp, mem_dir = bm25_only_components
        import dataclasses
        from uuid import uuid4

        from helpers import make_chunk

        winner = make_chunk(_WITH_PYTHON, source="race.md")
        await comp.storage.upsert_chunks([winner])

        # Everything the uniqueness index keys on is identical; only the id
        # differs — which is precisely the row ``INSERT OR IGNORE`` drops.
        loser = dataclasses.replace(winner, id=uuid4())
        assert loser.id != winner.id

        async with comp.storage.transaction():
            await comp.storage.upsert_chunks([loser])
            await comp.index_engine._extract_entities_for([loser])

        # The loser's id was never stored, so it must carry no entity rows —
        # and, the point of the test, nothing raised and the winner survived.
        assert await comp.storage.get_entities_for_chunk(str(loser.id)) == []
        assert await comp.storage.get_chunk(winner.id) is not None
