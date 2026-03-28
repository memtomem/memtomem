"""Tests for working memory (scratchpad)."""

import pytest
from datetime import datetime, timedelta, timezone


class TestScratch:
    @pytest.mark.asyncio
    async def test_set_and_get(self, storage):
        await storage.scratch_set("key1", "value1")
        entry = await storage.scratch_get("key1")
        assert entry is not None
        assert entry["value"] == "value1"

    @pytest.mark.asyncio
    async def test_get_nonexistent(self, storage):
        entry = await storage.scratch_get("nope")
        assert entry is None

    @pytest.mark.asyncio
    async def test_list(self, storage):
        await storage.scratch_set("a", "1")
        await storage.scratch_set("b", "2")
        items = await storage.scratch_list()
        assert len(items) == 2

    @pytest.mark.asyncio
    async def test_delete(self, storage):
        await storage.scratch_set("del_me", "gone")
        removed = await storage.scratch_delete("del_me")
        assert removed is True
        assert await storage.scratch_get("del_me") is None

    @pytest.mark.asyncio
    async def test_ttl_cleanup(self, storage):
        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds")
        await storage.scratch_set("expired", "old", expires_at=past)
        await storage.scratch_set("fresh", "new")
        cleaned = await storage.scratch_cleanup()
        assert cleaned == 1
        assert await storage.scratch_get("expired") is None
        assert await storage.scratch_get("fresh") is not None

    @pytest.mark.asyncio
    async def test_session_cleanup(self, storage):
        await storage.scratch_set("session_bound", "data", session_id="sess-1")
        await storage.scratch_set("global", "data")
        cleaned = await storage.scratch_cleanup(session_id="sess-1")
        assert cleaned == 1
        assert await storage.scratch_get("session_bound") is None
        assert await storage.scratch_get("global") is not None

    @pytest.mark.asyncio
    async def test_promoted_survives_cleanup(self, storage):
        await storage.scratch_set("important", "keep me", session_id="sess-2")
        db = storage._get_db()
        db.execute("UPDATE working_memory SET promoted = 1 WHERE key = 'important'")
        db.commit()
        await storage.scratch_cleanup(session_id="sess-2")
        entry = await storage.scratch_get("important")
        assert entry is not None

    @pytest.mark.asyncio
    async def test_overwrite(self, storage):
        await storage.scratch_set("key", "v1")
        await storage.scratch_set("key", "v2")
        entry = await storage.scratch_get("key")
        assert entry["value"] == "v2"
