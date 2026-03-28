"""Tests for storage backend operations."""

import pytest
from uuid import UUID, uuid4
from pathlib import Path
from memtomem.models import Chunk, ChunkMetadata

from helpers import make_chunk as _make_chunk


class TestChunkCRUD:
    @pytest.mark.asyncio
    async def test_upsert_and_get(self, storage):
        chunk = _make_chunk("hello world")
        await storage.upsert_chunks([chunk])
        result = await storage.get_chunk(chunk.id)
        assert result is not None
        assert result.content == "hello world"

    @pytest.mark.asyncio
    async def test_delete_chunks(self, storage):
        chunk = _make_chunk()
        await storage.upsert_chunks([chunk])
        deleted = await storage.delete_chunks([chunk.id])
        assert deleted == 1
        assert await storage.get_chunk(chunk.id) is None

    @pytest.mark.asyncio
    async def test_get_nonexistent(self, storage):
        result = await storage.get_chunk(uuid4())
        assert result is None


class TestTags:
    @pytest.mark.asyncio
    async def test_tag_counts(self, storage):
        c1 = _make_chunk(tags=("python", "debug"))
        c2 = _make_chunk(tags=("python", "web"))
        await storage.upsert_chunks([c1, c2])
        counts = await storage.get_tag_counts()
        tag_dict = dict(counts)
        assert tag_dict.get("python") == 2
        assert tag_dict.get("debug") == 1

    @pytest.mark.asyncio
    async def test_rename_tag(self, storage):
        chunk = _make_chunk(tags=("old_tag",))
        await storage.upsert_chunks([chunk])
        updated = await storage.rename_tag("old_tag", "new_tag")
        assert updated == 1
        counts = dict(await storage.get_tag_counts())
        assert "new_tag" in counts
        assert "old_tag" not in counts

    @pytest.mark.asyncio
    async def test_delete_tag(self, storage):
        chunk = _make_chunk(tags=("remove_me", "keep_me"))
        await storage.upsert_chunks([chunk])
        await storage.delete_tag("remove_me")
        counts = dict(await storage.get_tag_counts())
        assert "remove_me" not in counts
        assert "keep_me" in counts


class TestAccess:
    @pytest.mark.asyncio
    async def test_increment_and_get(self, storage):
        chunk = _make_chunk()
        await storage.upsert_chunks([chunk])
        await storage.increment_access([chunk.id])
        await storage.increment_access([chunk.id])
        counts = await storage.get_access_counts([chunk.id])
        assert counts[str(chunk.id)] == 2

    @pytest.mark.asyncio
    async def test_empty_access_counts(self, storage):
        counts = await storage.get_access_counts([])
        assert counts == {}


class TestRelations:
    @pytest.mark.asyncio
    async def test_add_and_get_related(self, storage):
        c1, c2 = _make_chunk(), _make_chunk()
        await storage.upsert_chunks([c1, c2])
        await storage.add_relation(c1.id, c2.id, "related")
        related = await storage.get_related(c1.id)
        assert len(related) == 1
        assert related[0][0] == c2.id

    @pytest.mark.asyncio
    async def test_bidirectional(self, storage):
        c1, c2 = _make_chunk(), _make_chunk()
        await storage.upsert_chunks([c1, c2])
        await storage.add_relation(c1.id, c2.id)
        # Query from either direction
        assert len(await storage.get_related(c1.id)) == 1
        assert len(await storage.get_related(c2.id)) == 1

    @pytest.mark.asyncio
    async def test_delete_relation(self, storage):
        c1, c2 = _make_chunk(), _make_chunk()
        await storage.upsert_chunks([c1, c2])
        await storage.add_relation(c1.id, c2.id)
        removed = await storage.delete_relation(c1.id, c2.id)
        assert removed is True
        assert len(await storage.get_related(c1.id)) == 0
