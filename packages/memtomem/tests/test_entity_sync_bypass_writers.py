"""Entity coverage for chunk writers that never reach the indexing engine (#2155).

#2145 put extraction on the indexer's write path. Two writers store content
without going through it: bundle import and consolidation summaries. Both were
pure coverage gaps — chunks whose content the extractor never saw. (Import cannot
strand *existing* entities: ``on_conflict="update"`` matches on ``content_hash``,
so the row it reuses holds the same content it already had.)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from helpers import make_chunk

from memtomem.tools.consolidation_engine import apply_consolidation
from memtomem.tools.export_import import export_chunks, import_chunks

# Distinct entity vocabularies so presence and absence are both checkable.
_PYTHON_TEXT = "We decided to use Python for the parser.\n"
_SQLITE_TEXT = "The sqlite migration is owned by Alice Kim.\n"
_NO_ENTITIES = "aaa bbb ccc\n"


async def _entity_values(comp) -> set[tuple[str, str]]:
    db = comp.storage._get_db()
    return {
        (r[0], r[1])
        for r in db.execute("SELECT entity_type, entity_value FROM chunk_entities").fetchall()
    }


def _bundle(tmp_path: Path, chunks: list[dict], name: str = "bundle.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps({"chunks": chunks}), encoding="utf-8")
    return path


def _record(chunk, content: str) -> dict:
    return {
        "id": str(chunk.id),
        "content": content,
        "content_hash": chunk.content_hash,
        "source_file": str(chunk.metadata.source_file),
        "namespace": chunk.metadata.namespace,
        "tags": list(chunk.metadata.tags),
        "chunk_type": chunk.metadata.chunk_type.value,
        "start_line": chunk.metadata.start_line,
        "end_line": chunk.metadata.end_line,
        "heading_hierarchy": list(chunk.metadata.heading_hierarchy),
        "language": chunk.metadata.language,
    }


class TestImportWritesEntities:
    @pytest.mark.asyncio
    async def test_imported_chunks_get_entities(self, bm25_only_components, tmp_path):
        comp, _ = bm25_only_components
        seed = make_chunk(_PYTHON_TEXT, source="imported.md")
        path = _bundle(tmp_path, [_record(seed, _PYTHON_TEXT)])

        stats = await import_chunks(comp.storage, comp.embedder, path)

        assert stats.imported_chunks == 1
        assert ("technology", "Python") in await _entity_values(comp)

    @pytest.mark.asyncio
    async def test_import_of_different_content_carries_its_own_entities(
        self, bm25_only_components, tmp_path
    ):
        """A bundle record whose content differs from anything stored becomes a
        new chunk — and that chunk is one the indexing engine never sees, so the
        import path is the only place its entities can come from.

        Note what ``on_conflict="update"`` does *not* do: it matches on
        ``content_hash``, so the row it reuses holds identical content. It never
        overwrites text out from under existing entity rows.
        """
        comp, mem_dir = bm25_only_components
        note = mem_dir / "note.md"
        note.write_text(_PYTHON_TEXT, encoding="utf-8")
        await comp.index_engine.index_file(note)

        stored = (await comp.storage.list_chunks_by_source(note))[0]
        path = _bundle(tmp_path, [_record(stored, _SQLITE_TEXT)])

        await import_chunks(
            comp.storage, comp.embedder, path, on_conflict="update", preserve_ids=True
        )

        values = await _entity_values(comp)
        # The indexed chunk keeps its own entities; the imported one gains its.
        assert ("technology", "Python") in values
        assert ("person", "Alice Kim") in values

    @pytest.mark.asyncio
    async def test_reimporting_a_real_bundle_does_not_duplicate_entities(
        self, bm25_only_components, tmp_path
    ):
        """Re-importing a store's own bundle leaves its entities exactly as they
        were: the records all hash-match, so the import adds no new chunk and
        writes no entities — no duplication, no churn.

        Uses a genuine ``export_chunks`` bundle rather than a hand-built record
        so the content hashes match what the store computed — a synthetic hash
        would silently import as a brand-new chunk and test nothing.
        """
        comp, mem_dir = bm25_only_components
        note = mem_dir / "note.md"
        note.write_text(_SQLITE_TEXT, encoding="utf-8")
        await comp.index_engine.index_file(note)
        before = await _entity_values(comp)
        assert before

        db = comp.storage._get_db()
        rows_before = db.execute("SELECT COUNT(*) FROM chunk_entities").fetchone()[0]

        bundle_path = tmp_path / "roundtrip.json"
        await export_chunks(comp.storage, output_path=bundle_path)
        stats = await import_chunks(comp.storage, comp.embedder, bundle_path, on_conflict="update")

        assert stats.updated_chunks >= 1, "expected the update branch, not a fresh insert"
        assert await _entity_values(comp) == before
        rows_after = db.execute("SELECT COUNT(*) FROM chunk_entities").fetchone()[0]
        assert rows_after == rows_before, "re-import duplicated entity rows"

    @pytest.mark.asyncio
    async def test_update_conflict_preserves_existing_entities(
        self, bm25_only_components, tmp_path
    ):
        """A hash-matched row keeps the entities it already had.

        The row holds content the store already has, and those entities may have
        come from ``mem_entity_scan``'s LLM pass, which is strictly richer than
        the regex extractor. Re-running the regex over them would delete-then-
        replace, quietly downgrading the better data — so import writes entities
        only for chunks it genuinely adds. Stood in for here by an entity no
        regex pattern produces.
        """
        comp, mem_dir = bm25_only_components
        note = mem_dir / "note.md"
        note.write_text(_SQLITE_TEXT, encoding="utf-8")
        await comp.index_engine.index_file(note)

        stored = (await comp.storage.list_chunks_by_source(note))[0]
        await comp.storage.upsert_entities(
            str(stored.id),
            [{"entity_type": "concept", "entity_value": "llm-only finding", "confidence": 0.95}],
        )

        bundle_path = tmp_path / "roundtrip.json"
        await export_chunks(comp.storage, output_path=bundle_path)
        await import_chunks(comp.storage, comp.embedder, bundle_path, on_conflict="update")

        assert ("concept", "llm-only finding") in await _entity_values(comp), (
            "import overwrote entities on a row whose content it did not change"
        )

    @pytest.mark.asyncio
    async def test_failed_entity_write_rolls_back_the_chunks(
        self, bm25_only_components, tmp_path, monkeypatch
    ):
        """Chunks and their entities land together or not at all.

        Without the shared transaction the chunk write commits first, so a
        failure in the entity write reports the import as failed while leaving
        its chunks in the store — the caller is told nothing happened and the
        store disagrees.
        """
        comp, _ = bm25_only_components
        seed = make_chunk(_PYTHON_TEXT, source="boom.md")
        path = _bundle(tmp_path, [_record(seed, _PYTHON_TEXT)])

        async def _explode(*_args, **_kwargs):
            raise RuntimeError("entity write failed")

        monkeypatch.setattr("memtomem.tools.export_import.sync_entities_for_chunks", _explode)

        stats = await import_chunks(comp.storage, comp.embedder, path)

        assert stats.imported_chunks == 0
        assert stats.failed_chunks == 1
        assert await comp.storage.recall_chunks(limit=10) == []

    @pytest.mark.asyncio
    async def test_extract_entities_false_writes_nothing(self, bm25_only_components, tmp_path):
        comp, _ = bm25_only_components
        seed = make_chunk(_PYTHON_TEXT, source="imported.md")
        path = _bundle(tmp_path, [_record(seed, _PYTHON_TEXT)])

        await import_chunks(comp.storage, comp.embedder, path, extract_entities=False)

        assert await _entity_values(comp) == set()

    @pytest.mark.asyncio
    async def test_entityless_import_stores_no_rows(self, bm25_only_components, tmp_path):
        comp, _ = bm25_only_components
        seed = make_chunk(_NO_ENTITIES, source="plain.md")
        path = _bundle(tmp_path, [_record(seed, _NO_ENTITIES)])

        await import_chunks(comp.storage, comp.embedder, path)

        assert await _entity_values(comp) == set()


class TestConsolidationSummaryGetsEntities:
    @pytest.mark.asyncio
    async def test_summary_chunk_carries_entities(self, bm25_only_components):
        """The summary is a virtual chunk that never passes through the engine,
        so without this call it could never have entities at all."""
        comp, _ = bm25_only_components
        originals = [make_chunk(_NO_ENTITIES, source="src.md") for _ in range(2)]
        await comp.storage.upsert_chunks(originals)

        summary_id = await apply_consolidation(
            comp.storage,
            {"source": "/tmp/src.md", "chunk_ids": [str(c.id) for c in originals]},
            _SQLITE_TEXT,
        )

        rows = await comp.storage.get_entities_for_chunk(str(summary_id))
        assert {(r["entity_type"], r["entity_value"]) for r in rows} >= {
            ("person", "Alice Kim"),
            ("technology", "sqlite"),
        }

    @pytest.mark.asyncio
    async def test_failed_entity_write_rolls_back_the_summary(
        self, bm25_only_components, monkeypatch
    ):
        """The summary and its entities land together or not at all.

        This one matters more than the import case it mirrors: a summary is
        idempotency-keyed by its source hash, so a summary that committed while
        its entity write failed would be skipped on every later run — the gap
        would never be retried.
        """
        comp, _ = bm25_only_components
        originals = [make_chunk(_NO_ENTITIES, source="boom.md") for _ in range(2)]
        await comp.storage.upsert_chunks(originals)
        before = len(await comp.storage.recall_chunks(limit=50))

        async def _explode(*_args, **_kwargs):
            raise RuntimeError("entity write failed")

        monkeypatch.setattr(
            "memtomem.tools.consolidation_engine.sync_entities_for_chunks", _explode
        )

        with pytest.raises(RuntimeError):
            await apply_consolidation(
                comp.storage,
                {"source": "/tmp/boom.md", "chunk_ids": [str(c.id) for c in originals]},
                _SQLITE_TEXT,
            )

        assert len(await comp.storage.recall_chunks(limit=50)) == before, (
            "the summary chunk survived a failed entity write"
        )
        assert await _entity_values(comp) == set()
