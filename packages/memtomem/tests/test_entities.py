"""Tests for EntityMixin storage methods."""

import pytest
from helpers import make_chunk

from memtomem.errors import StorageError


class TestEntityMixin:
    @pytest.mark.asyncio
    async def test_upsert_and_get(self, storage):
        chunk = make_chunk("Alice met Bob in 2024")
        await storage.upsert_chunks([chunk])

        entities = [
            {"entity_type": "person", "entity_value": "Alice", "confidence": 0.95, "position": 0},
            {"entity_type": "person", "entity_value": "Bob", "confidence": 0.9, "position": 1},
            {"entity_type": "date", "entity_value": "2024", "confidence": 1.0, "position": 2},
        ]
        count = await storage.upsert_entities(str(chunk.id), entities)
        assert count == 3

        result = await storage.get_entities_for_chunk(str(chunk.id))
        assert len(result) == 3
        assert result[0]["entity_value"] == "Alice"
        assert result[2]["entity_type"] == "date"

    @pytest.mark.asyncio
    async def test_upsert_overwrites(self, storage):
        chunk = make_chunk("some content")
        await storage.upsert_chunks([chunk])

        await storage.upsert_entities(
            str(chunk.id),
            [
                {"entity_type": "person", "entity_value": "Old"},
            ],
        )
        await storage.upsert_entities(
            str(chunk.id),
            [
                {"entity_type": "person", "entity_value": "New"},
            ],
        )

        result = await storage.get_entities_for_chunk(str(chunk.id))
        assert len(result) == 1
        assert result[0]["entity_value"] == "New"

    @pytest.mark.asyncio
    async def test_upsert_empty(self, storage):
        count = await storage.upsert_entities("nonexistent", [])
        assert count == 0

    @pytest.mark.asyncio
    async def test_upsert_rolls_back_on_insert_failure(self, storage):
        chunk = make_chunk("Alice met Bob")
        await storage.upsert_chunks([chunk])
        await storage.upsert_entities(
            str(chunk.id),
            [
                {"entity_type": "person", "entity_value": "Alice"},
                {"entity_type": "person", "entity_value": "Bob"},
            ],
        )

        # A second upsert whose INSERT fails on an unbindable value, AFTER the
        # DELETE has run. The pending DELETE must be rolled back — not left for a
        # later unrelated commit on the shared writer connection to flush (#1572).
        with pytest.raises(StorageError):
            await storage.upsert_entities(
                str(chunk.id),
                [{"entity_type": "person", "entity_value": object()}],
            )

        # Unrelated commit on the shared writer connection: would flush an
        # orphaned DELETE if one were left pending.
        await storage.increment_access([chunk.id])

        result = await storage.get_entities_for_chunk(str(chunk.id))
        assert {r["entity_value"] for r in result} == {"Alice", "Bob"}

    @pytest.mark.asyncio
    async def test_upsert_malformed_entity_preserves_existing(self, storage):
        chunk = make_chunk("keep me")
        await storage.upsert_chunks([chunk])
        await storage.upsert_entities(
            str(chunk.id),
            [{"entity_type": "person", "entity_value": "Keep"}],
        )

        # A batch with a missing required key is rejected before the DELETE, so
        # the existing entity is preserved rather than silently wiped (#1572).
        with pytest.raises(KeyError):
            await storage.upsert_entities(
                str(chunk.id),
                [{"entity_type": "person"}],  # no entity_value
            )

        await storage.increment_access([chunk.id])

        result = await storage.get_entities_for_chunk(str(chunk.id))
        assert [r["entity_value"] for r in result] == ["Keep"]

    @pytest.mark.asyncio
    async def test_delete_entities(self, storage):
        chunk = make_chunk("test")
        await storage.upsert_chunks([chunk])
        await storage.upsert_entities(
            str(chunk.id),
            [
                {"entity_type": "tech", "entity_value": "Python"},
            ],
        )
        deleted = await storage.delete_entities_for_chunk(str(chunk.id))
        assert deleted == 1

        result = await storage.get_entities_for_chunk(str(chunk.id))
        assert len(result) == 0

    @pytest.mark.asyncio
    async def test_search_by_type(self, storage):
        chunk = make_chunk("Alice uses Python")
        await storage.upsert_chunks([chunk])
        await storage.upsert_entities(
            str(chunk.id),
            [
                {"entity_type": "person", "entity_value": "Alice"},
                {"entity_type": "tech", "entity_value": "Python"},
            ],
        )

        results = await storage.search_entities(entity_type="person")
        assert len(results) == 1
        assert results[0]["entity_value"] == "Alice"

    @pytest.mark.asyncio
    async def test_search_by_value(self, storage):
        chunk = make_chunk("Bob in Paris")
        await storage.upsert_chunks([chunk])
        await storage.upsert_entities(
            str(chunk.id),
            [
                {"entity_type": "person", "entity_value": "Bob"},
                {"entity_type": "location", "entity_value": "Paris"},
            ],
        )

        results = await storage.search_entities(value="Par")
        assert len(results) == 1
        assert results[0]["entity_value"] == "Paris"

    @pytest.mark.asyncio
    async def test_search_by_namespace(self, storage):
        c1 = make_chunk("Alice", namespace="work")
        c2 = make_chunk("Bob", namespace="personal")
        await storage.upsert_chunks([c1, c2])
        await storage.upsert_entities(
            str(c1.id), [{"entity_type": "person", "entity_value": "Alice"}]
        )
        await storage.upsert_entities(
            str(c2.id), [{"entity_type": "person", "entity_value": "Bob"}]
        )

        results = await storage.search_entities(namespace="work")
        assert len(results) == 1
        assert results[0]["entity_value"] == "Alice"

    @pytest.mark.asyncio
    async def test_entity_type_counts(self, storage):
        chunk = make_chunk("test")
        await storage.upsert_chunks([chunk])
        await storage.upsert_entities(
            str(chunk.id),
            [
                {"entity_type": "person", "entity_value": "A"},
                {"entity_type": "person", "entity_value": "B"},
                {"entity_type": "tech", "entity_value": "C"},
            ],
        )

        counts = await storage.get_entity_type_counts()
        assert counts["person"] == 2
        assert counts["tech"] == 1
