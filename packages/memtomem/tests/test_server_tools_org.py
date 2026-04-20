"""Tests for server tool organization functions: namespace, tags, session, scratch."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from helpers import make_chunk
from memtomem.errors import StorageError


# ---------------------------------------------------------------------------
# Namespace tools
# ---------------------------------------------------------------------------


class TestNamespace:
    async def test_list_namespaces_empty(self, storage):
        result = await storage.list_namespaces()
        assert result == []

    async def test_list_namespaces_counts(self, storage):
        chunks = [
            make_chunk(content="a", namespace="proj-alpha"),
            make_chunk(content="b", namespace="proj-alpha"),
            make_chunk(content="c", namespace="proj-beta"),
        ]
        await storage.upsert_chunks(chunks)
        ns = dict(await storage.list_namespaces())
        assert ns["proj-alpha"] == 2
        assert ns["proj-beta"] == 1

    async def test_rename_namespace(self, storage):
        chunks = [
            make_chunk(content="one", namespace="old-ns"),
            make_chunk(content="two", namespace="old-ns"),
            make_chunk(content="other", namespace="keep-ns"),
        ]
        await storage.upsert_chunks(chunks)
        count = await storage.rename_namespace("old-ns", "new-ns")
        assert count == 2
        ns = dict(await storage.list_namespaces())
        assert "old-ns" not in ns
        assert ns["new-ns"] == 2
        assert ns["keep-ns"] == 1

    async def test_rename_nonexistent_namespace(self, storage):
        count = await storage.rename_namespace("ghost", "phantom")
        assert count == 0

    async def test_delete_by_namespace(self, storage):
        chunks = [
            make_chunk(content="del1", namespace="doomed"),
            make_chunk(content="del2", namespace="doomed"),
            make_chunk(content="safe", namespace="keeper"),
        ]
        await storage.upsert_chunks(chunks)
        deleted = await storage.delete_by_namespace("doomed")
        assert deleted == 2
        ns = dict(await storage.list_namespaces())
        assert "doomed" not in ns
        assert ns["keeper"] == 1

    async def test_delete_namespace_empty(self, storage):
        deleted = await storage.delete_by_namespace("nonexistent")
        assert deleted == 0

    async def test_set_and_get_namespace_meta(self, storage):
        await storage.set_namespace_meta("proj-x", description="Project X docs", color="#ff0000")
        meta = await storage.get_namespace_meta("proj-x")
        assert meta is not None
        assert meta["description"] == "Project X docs"
        assert meta["color"] == "#ff0000"
        assert meta["namespace"] == "proj-x"

    async def test_update_namespace_meta(self, storage):
        await storage.set_namespace_meta("proj-y", description="Original")
        await storage.set_namespace_meta("proj-y", description="Updated")
        meta = await storage.get_namespace_meta("proj-y")
        assert meta["description"] == "Updated"

    async def test_get_namespace_meta_nonexistent(self, storage):
        meta = await storage.get_namespace_meta("no-such-ns")
        assert meta is None

    async def test_namespace_meta_partial_update(self, storage):
        await storage.set_namespace_meta("ns-partial", description="desc", color="#000")
        await storage.set_namespace_meta("ns-partial", color="#fff")
        meta = await storage.get_namespace_meta("ns-partial")
        assert meta["description"] == "desc"
        assert meta["color"] == "#fff"

    async def test_namespace_assign_via_upsert(self, storage):
        """Verify chunks in different namespaces are tracked independently."""
        c1 = make_chunk(content="alpha chunk", namespace="ns-a")
        c2 = make_chunk(content="beta chunk", namespace="ns-b")
        c3 = make_chunk(content="another alpha", namespace="ns-a")
        await storage.upsert_chunks([c1, c2, c3])
        ns = dict(await storage.list_namespaces())
        assert ns["ns-a"] == 2
        assert ns["ns-b"] == 1

    @pytest.mark.parametrize("bad", ["bad name!", "no\ttab", 'quote"here', "a" * 256])
    async def test_set_namespace_meta_rejects_invalid(self, storage, bad):
        with pytest.raises(StorageError, match="Invalid namespace"):
            await storage.set_namespace_meta(bad, description="x")

    async def test_set_namespace_meta_accepts_agent_slash_form(self, storage):
        """``agent/{id}`` passes validation after widening ``_NS_NAME_RE``."""
        await storage.set_namespace_meta("agent/alpha", description="multi-agent ns")
        meta = await storage.get_namespace_meta("agent/alpha")
        assert meta is not None
        assert meta["namespace"] == "agent/alpha"

    async def test_rename_namespace_rejects_invalid_target(self, storage):
        chunks = [make_chunk(content="x", namespace="source-ns")]
        await storage.upsert_chunks(chunks)
        with pytest.raises(StorageError, match="Invalid namespace"):
            await storage.rename_namespace("source-ns", "bad!target")

    async def test_assign_namespace_rejects_invalid_target(self, storage):
        chunks = [make_chunk(content="x", namespace="orig")]
        await storage.upsert_chunks(chunks)
        with pytest.raises(StorageError, match="Invalid namespace"):
            await storage.assign_namespace("bad\tname", old_namespace="orig")


# ---------------------------------------------------------------------------
# Tag management tools
# ---------------------------------------------------------------------------


class TestTagManagement:
    async def test_get_tag_counts_empty(self, storage):
        counts = await storage.get_tag_counts()
        assert counts == []

    async def test_get_tag_counts(self, storage):
        c1 = make_chunk(content="a", tags=("python", "async"))
        c2 = make_chunk(content="b", tags=("python", "web"))
        c3 = make_chunk(content="c", tags=("rust",))
        await storage.upsert_chunks([c1, c2, c3])
        tag_dict = dict(await storage.get_tag_counts())
        assert tag_dict["python"] == 2
        assert tag_dict["async"] == 1
        assert tag_dict["web"] == 1
        assert tag_dict["rust"] == 1

    async def test_rename_tag(self, storage):
        c1 = make_chunk(content="a", tags=("legacy-tag", "other"))
        c2 = make_chunk(content="b", tags=("legacy-tag",))
        c3 = make_chunk(content="c", tags=("unrelated",))
        await storage.upsert_chunks([c1, c2, c3])
        renamed = await storage.rename_tag("legacy-tag", "modern-tag")
        assert renamed == 2
        tag_dict = dict(await storage.get_tag_counts())
        assert "legacy-tag" not in tag_dict
        assert tag_dict["modern-tag"] == 2
        assert tag_dict["other"] == 1
        assert tag_dict["unrelated"] == 1

    async def test_rename_tag_nonexistent(self, storage):
        renamed = await storage.rename_tag("ghost-tag", "new-tag")
        assert renamed == 0

    async def test_delete_tag(self, storage):
        c1 = make_chunk(content="a", tags=("remove-me", "keep-me"))
        c2 = make_chunk(content="b", tags=("remove-me",))
        await storage.upsert_chunks([c1, c2])
        deleted = await storage.delete_tag("remove-me")
        assert deleted == 2
        tag_dict = dict(await storage.get_tag_counts())
        assert "remove-me" not in tag_dict
        assert "keep-me" in tag_dict

    async def test_delete_tag_preserves_chunks(self, storage):
        """Deleting a tag should not delete the chunks themselves."""
        chunk = make_chunk(content="important content", tags=("disposable-tag",))
        await storage.upsert_chunks([chunk])
        await storage.delete_tag("disposable-tag")
        result = await storage.get_chunk(chunk.id)
        assert result is not None
        assert result.content == "important content"

    async def test_delete_tag_nonexistent(self, storage):
        deleted = await storage.delete_tag("no-such-tag")
        assert deleted == 0

    async def test_rename_tag_deduplicates(self, storage):
        """Renaming a tag that merges with an existing tag deduplicates."""
        chunk = make_chunk(content="a", tags=("tag-a", "tag-b"))
        await storage.upsert_chunks([chunk])
        await storage.rename_tag("tag-a", "tag-b")
        tag_dict = dict(await storage.get_tag_counts())
        assert tag_dict.get("tag-b") == 1
        assert "tag-a" not in tag_dict


# ---------------------------------------------------------------------------
# Session (episodic memory) tools
# ---------------------------------------------------------------------------


class TestSessions:
    async def test_create_and_list(self, storage):
        await storage.create_session("sess-1", "agent-a", "default")
        sessions = await storage.list_sessions(agent_id="agent-a")
        assert len(sessions) == 1
        assert sessions[0]["id"] == "sess-1"
        assert sessions[0]["agent_id"] == "agent-a"
        assert sessions[0]["namespace"] == "default"
        assert sessions[0]["ended_at"] is None

    async def test_end_session(self, storage):
        await storage.create_session("sess-2", "agent-b", "work")
        await storage.end_session("sess-2", "Completed analysis", {"queries": 5})
        sessions = await storage.list_sessions(agent_id="agent-b")
        assert sessions[0]["ended_at"] is not None
        assert sessions[0]["summary"] == "Completed analysis"

    async def test_add_and_get_session_events(self, storage):
        await storage.create_session("sess-3", "agent-c", "default")
        await storage.add_session_event("sess-3", "query", "search for X")
        await storage.add_session_event("sess-3", "add", "added chunk Y", ["chunk-1", "chunk-2"])
        await storage.add_session_event("sess-3", "note", "observation Z")
        events = await storage.get_session_events("sess-3")
        assert len(events) == 3
        assert events[0]["event_type"] == "query"
        assert events[0]["content"] == "search for X"
        assert events[0]["chunk_ids"] == []
        assert events[1]["event_type"] == "add"
        assert events[1]["chunk_ids"] == ["chunk-1", "chunk-2"]
        assert events[2]["event_type"] == "note"

    async def test_duplicate_session_ignored(self, storage):
        await storage.create_session("dup-id", "agent-1", "ns-a")
        await storage.create_session("dup-id", "agent-2", "ns-b")
        sessions = await storage.list_sessions()
        dup = [s for s in sessions if s["id"] == "dup-id"]
        assert len(dup) == 1
        assert dup[0]["agent_id"] == "agent-1"

    async def test_list_sessions_with_limit(self, storage):
        for i in range(5):
            await storage.create_session(f"lim-{i}", "agent", "default")
        sessions = await storage.list_sessions(agent_id="agent", limit=3)
        assert len(sessions) == 3

    async def test_list_sessions_with_since_filter(self, storage):
        await storage.create_session("old-s", "agent", "default")
        sessions = await storage.list_sessions(since="2099-01-01T00:00:00+00:00")
        assert len(sessions) == 0

    async def test_session_with_metadata(self, storage):
        meta = {"title": "Debug session", "tags": ["bug", "urgent"]}
        await storage.create_session("meta-s", "agent-d", "default", metadata=meta)
        sessions = await storage.list_sessions(agent_id="agent-d")
        assert len(sessions) == 1
        assert sessions[0]["id"] == "meta-s"

    async def test_get_events_empty_session(self, storage):
        await storage.create_session("empty-s", "agent", "default")
        events = await storage.get_session_events("empty-s")
        assert events == []

    async def test_multiple_agents(self, storage):
        await storage.create_session("s-a1", "agent-x", "default")
        await storage.create_session("s-a2", "agent-x", "default")
        await storage.create_session("s-b1", "agent-y", "default")
        x_sessions = await storage.list_sessions(agent_id="agent-x")
        y_sessions = await storage.list_sessions(agent_id="agent-y")
        assert len(x_sessions) == 2
        assert len(y_sessions) == 1


# ---------------------------------------------------------------------------
# Scratch (working memory) tools
# ---------------------------------------------------------------------------


class TestScratch:
    async def test_set_and_get(self, storage):
        await storage.scratch_set("my-key", "my-value")
        entry = await storage.scratch_get("my-key")
        assert entry is not None
        assert entry["key"] == "my-key"
        assert entry["value"] == "my-value"
        assert entry["promoted"] is False

    async def test_get_nonexistent(self, storage):
        entry = await storage.scratch_get("nonexistent-key")
        assert entry is None

    async def test_list_all(self, storage):
        await storage.scratch_set("k1", "v1")
        await storage.scratch_set("k2", "v2")
        await storage.scratch_set("k3", "v3")
        items = await storage.scratch_list()
        assert len(items) == 3
        keys = {item["key"] for item in items}
        assert keys == {"k1", "k2", "k3"}

    async def test_list_by_session(self, storage):
        await storage.scratch_set("s-key1", "val1", session_id="sess-a")
        await storage.scratch_set("s-key2", "val2", session_id="sess-a")
        await storage.scratch_set("global-key", "val3")
        session_items = await storage.scratch_list(session_id="sess-a")
        assert len(session_items) == 2
        all_items = await storage.scratch_list()
        assert len(all_items) == 3

    async def test_delete(self, storage):
        await storage.scratch_set("del-target", "data")
        removed = await storage.scratch_delete("del-target")
        assert removed is True
        assert await storage.scratch_get("del-target") is None

    async def test_delete_nonexistent(self, storage):
        removed = await storage.scratch_delete("no-such-key")
        assert removed is False

    async def test_overwrite_value(self, storage):
        await storage.scratch_set("mutable", "version-1")
        await storage.scratch_set("mutable", "version-2")
        entry = await storage.scratch_get("mutable")
        assert entry["value"] == "version-2"

    async def test_session_bound_cleanup(self, storage):
        await storage.scratch_set("bound", "data", session_id="sess-clean")
        await storage.scratch_set("free", "data")
        cleaned = await storage.scratch_cleanup(session_id="sess-clean")
        assert cleaned == 1
        assert await storage.scratch_get("bound") is None
        assert await storage.scratch_get("free") is not None

    async def test_ttl_expired_cleanup(self, storage):
        past = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(timespec="seconds")
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds")
        await storage.scratch_set("expired", "old", expires_at=past)
        await storage.scratch_set("still-valid", "new", expires_at=future)
        await storage.scratch_set("no-ttl", "permanent")
        cleaned = await storage.scratch_cleanup()
        assert cleaned == 1
        assert await storage.scratch_get("expired") is None
        assert await storage.scratch_get("still-valid") is not None
        assert await storage.scratch_get("no-ttl") is not None

    async def test_promoted_survives_session_cleanup(self, storage):
        await storage.scratch_set("important", "keep", session_id="sess-prom")
        promoted = await storage.scratch_promote("important")
        assert promoted is True
        cleaned = await storage.scratch_cleanup(session_id="sess-prom")
        assert cleaned == 0
        entry = await storage.scratch_get("important")
        assert entry is not None
        assert entry["promoted"] is True

    async def test_promoted_survives_ttl_cleanup(self, storage):
        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds")
        await storage.scratch_set("prom-ttl", "data", expires_at=past)
        await storage.scratch_promote("prom-ttl")
        cleaned = await storage.scratch_cleanup()
        assert cleaned == 0
        assert await storage.scratch_get("prom-ttl") is not None

    async def test_promote_nonexistent(self, storage):
        promoted = await storage.scratch_promote("no-key")
        assert promoted is False

    async def test_scratch_with_session_id(self, storage):
        await storage.scratch_set("ctx", "session context", session_id="sess-ctx")
        entry = await storage.scratch_get("ctx")
        assert entry["session_id"] == "sess-ctx"
