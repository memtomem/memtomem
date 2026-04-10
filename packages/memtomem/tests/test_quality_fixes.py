"""Tests for quality audit fixes: read pool, batch ops, error handler, linking, sessions."""

import pytest


from helpers import make_chunk as _make_chunk


# ── Read Pool ─────────────────────────────────────────────────────────


class TestReadPool:
    @pytest.mark.asyncio
    async def test_read_pool_initialized(self, storage):
        """Read pool should have 3 connections after init."""
        assert len(storage._read_pool) == 3

    @pytest.mark.asyncio
    async def test_get_read_db_round_robins(self, storage):
        """_get_read_db() should cycle through pool connections."""
        c1 = storage._get_read_db()
        c2 = storage._get_read_db()
        c3 = storage._get_read_db()
        c4 = storage._get_read_db()  # wraps around
        assert c1 is not c2
        assert c2 is not c3
        assert c4 is c1  # round-robin

    @pytest.mark.asyncio
    async def test_read_pool_used_for_search(self, storage):
        """bm25_search and dense_search should use read pool, not write conn."""
        chunk = _make_chunk("hello world search test")
        await storage.upsert_chunks([chunk])

        # bm25_search should use read pool
        results = await storage.bm25_search("hello", top_k=5)
        assert len(results) >= 1

    @pytest.mark.asyncio
    async def test_get_chunk_uses_read_pool(self, storage):
        """get_chunk should succeed via read pool."""
        chunk = _make_chunk("readable chunk")
        await storage.upsert_chunks([chunk])
        result = await storage.get_chunk(chunk.id)
        assert result is not None
        assert result.content == "readable chunk"

    @pytest.mark.asyncio
    async def test_get_stats_uses_read_pool(self, storage):
        """get_stats should work via read pool."""
        chunk = _make_chunk()
        await storage.upsert_chunks([chunk])
        stats = await storage.get_stats()
        assert stats["total_chunks"] == 1


# ── Batch Operations ──────────────────────────────────────────────────


class TestBatchOperations:
    @pytest.mark.asyncio
    async def test_increment_access_batch(self, storage):
        """increment_access should batch-update multiple chunks."""
        c1 = _make_chunk("batch1", source="b1.md")
        c2 = _make_chunk("batch2", source="b2.md")
        c3 = _make_chunk("batch3", source="b3.md")
        await storage.upsert_chunks([c1, c2, c3])

        await storage.increment_access([c1.id, c2.id, c3.id])
        counts = await storage.get_access_counts([c1.id, c2.id, c3.id])
        assert counts[str(c1.id)] == 1
        assert counts[str(c2.id)] == 1
        assert counts[str(c3.id)] == 1

    @pytest.mark.asyncio
    async def test_increment_access_empty(self, storage):
        """increment_access with empty list should not error."""
        await storage.increment_access([])

    @pytest.mark.asyncio
    async def test_increment_access_multiple_times(self, storage):
        """Repeated calls should accumulate correctly."""
        chunk = _make_chunk()
        await storage.upsert_chunks([chunk])
        await storage.increment_access([chunk.id])
        await storage.increment_access([chunk.id])
        await storage.increment_access([chunk.id])
        counts = await storage.get_access_counts([chunk.id])
        assert counts[str(chunk.id)] == 3

    @pytest.mark.asyncio
    async def test_update_importance_scores_batch(self, storage):
        """update_importance_scores should batch-update all in one call."""
        c1 = _make_chunk("imp1", source="i1.md")
        c2 = _make_chunk("imp2", source="i2.md")
        await storage.upsert_chunks([c1, c2])

        updated = await storage.update_importance_scores({
            str(c1.id): 0.8,
            str(c2.id): 0.3,
        })
        assert updated == 2

        scores = await storage.get_importance_scores([c1.id, c2.id])
        assert scores[str(c1.id)] == pytest.approx(0.8)
        assert scores[str(c2.id)] == pytest.approx(0.3)

    @pytest.mark.asyncio
    async def test_update_importance_scores_empty(self, storage):
        """Empty scores dict should return 0."""
        result = await storage.update_importance_scores({})
        assert result == 0

    @pytest.mark.asyncio
    async def test_rename_tag_batch(self, storage):
        """rename_tag should batch-update all chunks with the old tag."""
        c1 = _make_chunk("t1", tags=("alpha", "beta"), source="t1.md")
        c2 = _make_chunk("t2", tags=("alpha", "gamma"), source="t2.md")
        c3 = _make_chunk("t3", tags=("gamma",), source="t3.md")
        await storage.upsert_chunks([c1, c2, c3])

        renamed = await storage.rename_tag("alpha", "omega")
        assert renamed == 2

        # Verify tags updated
        r1 = await storage.get_chunk(c1.id)
        r2 = await storage.get_chunk(c2.id)
        r3 = await storage.get_chunk(c3.id)
        assert "omega" in r1.metadata.tags
        assert "alpha" not in r1.metadata.tags
        assert "omega" in r2.metadata.tags
        assert "gamma" in r3.metadata.tags

    @pytest.mark.asyncio
    async def test_delete_tag_batch(self, storage):
        """delete_tag should batch-remove tag from all chunks."""
        c1 = _make_chunk("d1", tags=("remove_me", "keep"), source="d1.md")
        c2 = _make_chunk("d2", tags=("remove_me",), source="d2.md")
        await storage.upsert_chunks([c1, c2])

        deleted = await storage.delete_tag("remove_me")
        assert deleted == 2

        r1 = await storage.get_chunk(c1.id)
        r2 = await storage.get_chunk(c2.id)
        assert "remove_me" not in r1.metadata.tags
        assert "keep" in r1.metadata.tags
        assert "remove_me" not in r2.metadata.tags


# ── Error Handler ─────────────────────────────────────────────────────


class TestErrorHandler:
    @pytest.mark.asyncio
    async def test_value_error_shows_message(self):
        from memtomem.server.error_handler import tool_handler

        @tool_handler
        async def bad_tool():
            raise ValueError("bad input")

        result = await bad_tool()
        assert result == "Error: bad input"

    @pytest.mark.asyncio
    async def test_key_error_shows_message(self):
        from memtomem.server.error_handler import tool_handler

        @tool_handler
        async def bad_tool():
            raise KeyError("missing_field")

        result = await bad_tool()
        assert "missing_field" in result
        assert result.startswith("Error:")

    @pytest.mark.asyncio
    async def test_file_not_found_shows_message(self):
        from memtomem.server.error_handler import tool_handler

        @tool_handler
        async def bad_tool():
            raise FileNotFoundError("/tmp/missing.md")

        result = await bad_tool()
        assert "/tmp/missing.md" in result

    @pytest.mark.asyncio
    async def test_type_error_shows_message(self):
        from memtomem.server.error_handler import tool_handler

        @tool_handler
        async def bad_tool():
            raise TypeError("expected str, got int")

        result = await bad_tool()
        assert "expected str, got int" in result

    @pytest.mark.asyncio
    async def test_unknown_error_includes_details(self):
        from memtomem.server.error_handler import tool_handler

        @tool_handler
        async def bad_tool():
            raise RuntimeError("something broke")

        result = await bad_tool()
        assert "RuntimeError" in result
        assert "something broke" in result

    @pytest.mark.asyncio
    async def test_embedding_error_shows_message(self):
        from memtomem.errors import EmbeddingError
        from memtomem.server.error_handler import tool_handler

        @tool_handler
        async def bad_tool():
            raise EmbeddingError("Ollama unreachable")

        result = await bad_tool()
        assert result == "Error: Ollama unreachable"

    @pytest.mark.asyncio
    async def test_connection_error_shows_message(self):
        from memtomem.server.error_handler import tool_handler

        @tool_handler
        async def bad_tool():
            raise ConnectionError("connection refused")

        result = await bad_tool()
        assert result == "Error: connection refused"

    @pytest.mark.asyncio
    async def test_timeout_error_shows_message(self):
        from memtomem.server.error_handler import tool_handler

        @tool_handler
        async def bad_tool():
            raise TimeoutError("request timed out")

        result = await bad_tool()
        assert result == "Error: request timed out"

    @pytest.mark.asyncio
    async def test_retryable_error_tagged(self):
        from memtomem.errors import RetryableError
        from memtomem.server.error_handler import tool_handler

        @tool_handler
        async def bad_tool():
            raise RetryableError("rate limited")

        result = await bad_tool()
        assert result == "Error (retryable): rate limited"

    @pytest.mark.asyncio
    async def test_permanent_error_tagged(self):
        from memtomem.errors import PermanentError
        from memtomem.server.error_handler import tool_handler

        @tool_handler
        async def bad_tool():
            raise PermanentError("invalid API key")

        result = await bad_tool()
        assert result == "Error (permanent): invalid API key"


# ── Session Duplicate Handling ────────────────────────────────────────


class TestSessionDuplicate:
    @pytest.mark.asyncio
    async def test_duplicate_session_ignored(self, storage):
        """Creating same session_id twice should not raise."""
        await storage.create_session("dup-1", "agent-a", "default")
        await storage.create_session("dup-1", "agent-a", "default")
        sessions = await storage.list_sessions()
        dup_sessions = [s for s in sessions if s["id"] == "dup-1"]
        assert len(dup_sessions) == 1


# ── Cross-Reference Linking ──────────────────────────────────────────


class TestCrossRefLinking:
    @pytest.mark.asyncio
    async def test_add_relation_different_ids(self, storage):
        """add_relation should link two different chunks."""
        c1 = _make_chunk("source chunk", source="s.md")
        c2 = _make_chunk("target chunk", source="t.md")
        await storage.upsert_chunks([c1, c2])

        await storage.add_relation(c1.id, c2.id, "consolidated_into")
        related = await storage.get_related(c1.id)
        assert len(related) == 1
        assert related[0][0] == c2.id
        assert related[0][1] == "consolidated_into"

    @pytest.mark.asyncio
    async def test_relation_bidirectional_query(self, storage):
        """get_related should find relations in both directions."""
        c1 = _make_chunk("a", source="a.md")
        c2 = _make_chunk("b", source="b.md")
        await storage.upsert_chunks([c1, c2])

        await storage.add_relation(c1.id, c2.id, "informs_reflection")
        # Query from target side
        related = await storage.get_related(c2.id)
        assert len(related) == 1
        assert related[0][0] == c1.id

    @pytest.mark.asyncio
    async def test_multiple_relations(self, storage):
        """Multiple chunks can be linked to one summary."""
        summary = _make_chunk("summary", source="sum.md")
        c1 = _make_chunk("orig1", source="o1.md")
        c2 = _make_chunk("orig2", source="o2.md")
        c3 = _make_chunk("orig3", source="o3.md")
        await storage.upsert_chunks([summary, c1, c2, c3])

        for c in [c1, c2, c3]:
            await storage.add_relation(c.id, summary.id, "consolidated_into")

        related = await storage.get_related(summary.id)
        assert len(related) == 3

    @pytest.mark.asyncio
    async def test_delete_relation(self, storage):
        """delete_relation should remove the link."""
        c1 = _make_chunk("x", source="x.md")
        c2 = _make_chunk("y", source="y.md")
        await storage.upsert_chunks([c1, c2])

        await storage.add_relation(c1.id, c2.id, "related")
        assert len(await storage.get_related(c1.id)) == 1

        removed = await storage.delete_relation(c1.id, c2.id)
        assert removed is True
        assert len(await storage.get_related(c1.id)) == 0


# ── Health Report Consolidated Query ──────────────────────────────────


class TestHealthReportConsolidated:
    @pytest.mark.asyncio
    async def test_counts_consistent(self, storage):
        """Health report aggregate counts should match individual queries."""
        c1 = _make_chunk("hr1", tags=("tag1",), source="hr1.md")
        c2 = _make_chunk("hr2", tags=(), source="hr2.md")
        c3 = _make_chunk("hr3", tags=("tag2", "tag3"), source="hr3.md")
        await storage.upsert_chunks([c1, c2, c3])
        await storage.increment_access([c1.id, c3.id])

        report = await storage.get_health_report()
        assert report["total_chunks"] == 3
        assert report["access_coverage"]["accessed"] == 2
        assert report["tag_coverage"]["tagged"] == 2
        assert report["dead_memories_pct"] == pytest.approx(33.3, abs=0.1)

    @pytest.mark.asyncio
    async def test_session_and_scratch_counts(self, storage):
        """Health report should include session and scratch counts."""
        await storage.create_session("hr-s1", "agent", "default")
        await storage.scratch_set("key1", "val1")
        await storage.scratch_set("key2", "val2")

        report = await storage.get_health_report()
        assert report["sessions"]["total"] >= 1
        assert report["working_memory"]["total"] >= 2

    @pytest.mark.asyncio
    async def test_relation_count(self, storage):
        """Health report should count cross-references."""
        c1 = _make_chunk("rel1", source="rel1.md")
        c2 = _make_chunk("rel2", source="rel2.md")
        await storage.upsert_chunks([c1, c2])
        await storage.add_relation(c1.id, c2.id, "related")

        report = await storage.get_health_report()
        assert report["cross_references"] == 1


# ── DB Indexes ────────────────────────────────────────────────────────


class TestDBIndexes:
    @pytest.mark.asyncio
    async def test_access_count_index_exists(self, storage):
        """access_count index should exist for analytics queries."""
        db = storage._get_db()
        indexes = db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_chunks_access_count'"
        ).fetchall()
        assert len(indexes) == 1

    @pytest.mark.asyncio
    async def test_importance_index_exists(self, storage):
        """importance_score index should exist for search pipeline."""
        db = storage._get_db()
        indexes = db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_chunks_importance'"
        ).fetchall()
        assert len(indexes) == 1


# ── Foreign Keys Enabled ─────────────────────────────────────────────


class TestForeignKeys:
    @pytest.mark.asyncio
    async def test_foreign_keys_pragma_enabled(self, storage):
        """PRAGMA foreign_keys should be ON for CASCADE to work."""
        db = storage._get_db()
        result = db.execute("PRAGMA foreign_keys").fetchone()
        assert result[0] == 1


# ── Datetime Timezone Normalization ──────────────────────────────────


class TestDatetimeNormalization:
    @pytest.mark.asyncio
    async def test_naive_datetime_gets_utc(self, storage):
        """Chunks with naive datetime stored should get UTC tzinfo on read."""
        from datetime import timezone
        chunk = _make_chunk("tz test")
        await storage.upsert_chunks([chunk])

        # Manually set a naive datetime in DB
        db = storage._get_db()
        db.execute(
            "UPDATE chunks SET created_at = '2024-01-01T12:00:00' WHERE id = ?",
            (str(chunk.id),),
        )
        db.commit()

        result = await storage.get_chunk(chunk.id)
        assert result is not None
        assert result.created_at.tzinfo is not None
        assert result.created_at.tzinfo == timezone.utc


# ── Formatter Truncation Indicator ───────────────────────────────────


class TestFormatterTruncation:
    def test_short_content_no_ellipsis(self):
        """Content under 500 chars should not get '...' appended."""
        from memtomem.server.formatters import _format_compact_result

        class FakeChunkMeta:
            heading_hierarchy = []
            namespace = "default"
            source_file = "/tmp/test.md"

        class FakeChunk:
            content = "short content"
            metadata = FakeChunkMeta()
            id = "test-id"

        class FakeResult:
            chunk = FakeChunk()
            score = 0.95
            rank = 1
            context = None

        result = _format_compact_result(FakeResult())
        assert not result.endswith("...")

    def test_long_content_has_ellipsis(self):
        """Content over 500 chars should get '...' appended."""
        from memtomem.server.formatters import _format_compact_result

        class FakeChunkMeta:
            heading_hierarchy = []
            namespace = "default"
            source_file = "/tmp/test.md"

        class FakeChunk:
            content = "x" * 600
            metadata = FakeChunkMeta()
            id = "test-id"

        class FakeResult:
            chunk = FakeChunk()
            score = 0.95
            rank = 1
            context = None

        result = _format_compact_result(FakeResult())
        assert result.endswith("...")
