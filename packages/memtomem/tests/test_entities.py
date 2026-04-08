"""Tests for EntityMixin storage methods."""

import pytest
from helpers import make_chunk


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

        await storage.upsert_entities(str(chunk.id), [
            {"entity_type": "person", "entity_value": "Old"},
        ])
        await storage.upsert_entities(str(chunk.id), [
            {"entity_type": "person", "entity_value": "New"},
        ])

        result = await storage.get_entities_for_chunk(str(chunk.id))
        assert len(result) == 1
        assert result[0]["entity_value"] == "New"

    @pytest.mark.asyncio
    async def test_upsert_empty(self, storage):
        count = await storage.upsert_entities("nonexistent", [])
        assert count == 0

    @pytest.mark.asyncio
    async def test_delete_entities(self, storage):
        chunk = make_chunk("test")
        await storage.upsert_chunks([chunk])
        await storage.upsert_entities(str(chunk.id), [
            {"entity_type": "tech", "entity_value": "Python"},
        ])
        deleted = await storage.delete_entities_for_chunk(str(chunk.id))
        assert deleted == 1

        result = await storage.get_entities_for_chunk(str(chunk.id))
        assert len(result) == 0

    @pytest.mark.asyncio
    async def test_search_by_type(self, storage):
        chunk = make_chunk("Alice uses Python")
        await storage.upsert_chunks([chunk])
        await storage.upsert_entities(str(chunk.id), [
            {"entity_type": "person", "entity_value": "Alice"},
            {"entity_type": "tech", "entity_value": "Python"},
        ])

        results = await storage.search_entities(entity_type="person")
        assert len(results) == 1
        assert results[0]["entity_value"] == "Alice"

    @pytest.mark.asyncio
    async def test_search_by_value(self, storage):
        chunk = make_chunk("Bob in Paris")
        await storage.upsert_chunks([chunk])
        await storage.upsert_entities(str(chunk.id), [
            {"entity_type": "person", "entity_value": "Bob"},
            {"entity_type": "location", "entity_value": "Paris"},
        ])

        results = await storage.search_entities(value="Par")
        assert len(results) == 1
        assert results[0]["entity_value"] == "Paris"

    @pytest.mark.asyncio
    async def test_search_by_namespace(self, storage):
        c1 = make_chunk("Alice", namespace="work")
        c2 = make_chunk("Bob", namespace="personal")
        await storage.upsert_chunks([c1, c2])
        await storage.upsert_entities(str(c1.id), [{"entity_type": "person", "entity_value": "Alice"}])
        await storage.upsert_entities(str(c2.id), [{"entity_type": "person", "entity_value": "Bob"}])

        results = await storage.search_entities(namespace="work")
        assert len(results) == 1
        assert results[0]["entity_value"] == "Alice"

    @pytest.mark.asyncio
    async def test_entity_type_counts(self, storage):
        chunk = make_chunk("test")
        await storage.upsert_chunks([chunk])
        await storage.upsert_entities(str(chunk.id), [
            {"entity_type": "person", "entity_value": "A"},
            {"entity_type": "person", "entity_value": "B"},
            {"entity_type": "tech", "entity_value": "C"},
        ])

        counts = await storage.get_entity_type_counts()
        assert counts["person"] == 2
        assert counts["tech"] == 1
