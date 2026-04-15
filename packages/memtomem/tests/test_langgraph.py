"""Tests for LangGraph adapter (MemtomemStore)."""

from unittest.mock import AsyncMock, MagicMock

import pytest


class TestMemtomemStoreInit:
    def test_default_init(self):
        from memtomem.integrations.langgraph import MemtomemStore

        store = MemtomemStore()
        assert store._components is None
        assert store._config_overrides == {}

    def test_config_overrides(self):
        from memtomem.integrations.langgraph import MemtomemStore

        store = MemtomemStore(
            config_overrides={
                "storage": {"sqlite_path": "/tmp/test.db"},
            }
        )
        assert store._config_overrides["storage"]["sqlite_path"] == "/tmp/test.db"

    def test_session_id_none_initially(self):
        from memtomem.integrations.langgraph import MemtomemStore

        store = MemtomemStore()
        assert store._current_session_id is None


class TestMemtomemStoreIndex:
    """Regression tests for MemtomemStore.index() — ensures it delegates to
    the correct IndexEngine API (previously called a nonexistent
    `index_directory` method)."""

    @pytest.mark.asyncio
    async def test_index_delegates_to_index_path(self, tmp_path):
        from memtomem.integrations.langgraph import MemtomemStore
        from memtomem.models import IndexingStats

        store = MemtomemStore()

        mock_engine = MagicMock()
        mock_engine.index_path = AsyncMock(
            return_value=IndexingStats(
                total_files=2,
                total_chunks=5,
                indexed_chunks=5,
                skipped_chunks=0,
                deleted_chunks=0,
                duration_ms=123.0,
            )
        )
        store._components = MagicMock(index_engine=mock_engine)

        result = await store.index(path=str(tmp_path), recursive=True, namespace="notes")

        mock_engine.index_path.assert_awaited_once()
        args, kwargs = mock_engine.index_path.call_args
        # Positional path argument is resolved to an absolute Path
        assert args[0] == tmp_path.expanduser().resolve()
        assert kwargs["recursive"] is True
        assert kwargs["namespace"] == "notes"

        assert result == {
            "total_files": 2,
            "indexed_chunks": 5,
            "duration_ms": 123.0,
        }

    @pytest.mark.asyncio
    async def test_index_engine_has_index_path(self):
        """Guards against renames of the target method on IndexEngine."""
        from memtomem.indexing.engine import IndexEngine

        assert hasattr(IndexEngine, "index_path"), (
            "IndexEngine.index_path is the target of MemtomemStore.index(); "
            "renaming it without updating the adapter will break LangGraph integration."
        )


@pytest.mark.ollama
class TestMemtomemStoreIntegration:
    @pytest.mark.asyncio
    async def test_lifecycle(self, tmp_path):
        """Test init, add, search, close lifecycle."""
        import json
        import os

        db_path = str(tmp_path / "test.db")
        mem_dir = str(tmp_path / "memories")
        (tmp_path / "memories").mkdir()

        os.environ["MEMTOMEM_STORAGE__SQLITE_PATH"] = db_path
        os.environ["MEMTOMEM_INDEXING__MEMORY_DIRS"] = json.dumps([mem_dir])
        os.environ["MEMTOMEM_EMBEDDING__MODEL"] = "bge-m3"
        os.environ["MEMTOMEM_EMBEDDING__DIMENSION"] = "1024"

        # Prevent ~/.memtomem/config.json from overriding test settings
        import memtomem.config as _cfg

        _orig_load = _cfg.load_config_overrides
        _cfg.load_config_overrides = lambda c: None

        try:
            from memtomem.integrations.langgraph import MemtomemStore

            async with MemtomemStore() as store:
                # Add
                result = await store.add("Test memory content", title="Test", tags=["test"])
                assert result["indexed_chunks"] >= 1

                # Search
                results = await store.search("test memory")
                assert isinstance(results, list)

                # Scratch
                await store.scratch_set("key1", "value1")
                val = await store.scratch_get("key1")
                assert val == "value1"

                entries = await store.scratch_list()
                assert len(entries) >= 1

        finally:
            _cfg.load_config_overrides = _orig_load
            for key in (
                "MEMTOMEM_STORAGE__SQLITE_PATH",
                "MEMTOMEM_INDEXING__MEMORY_DIRS",
                "MEMTOMEM_EMBEDDING__MODEL",
                "MEMTOMEM_EMBEDDING__DIMENSION",
            ):
                os.environ.pop(key, None)

    @pytest.mark.asyncio
    async def test_session_lifecycle(self, tmp_path):
        """Test session start and end."""
        import json
        import os

        db_path = str(tmp_path / "test.db")
        mem_dir = str(tmp_path / "memories")
        (tmp_path / "memories").mkdir()

        os.environ["MEMTOMEM_STORAGE__SQLITE_PATH"] = db_path
        os.environ["MEMTOMEM_INDEXING__MEMORY_DIRS"] = json.dumps([mem_dir])
        os.environ["MEMTOMEM_EMBEDDING__MODEL"] = "bge-m3"
        os.environ["MEMTOMEM_EMBEDDING__DIMENSION"] = "1024"

        import memtomem.config as _cfg

        _orig_load = _cfg.load_config_overrides
        _cfg.load_config_overrides = lambda c: None

        try:
            from memtomem.integrations.langgraph import MemtomemStore

            async with MemtomemStore() as store:
                session_id = await store.start_session(agent_id="test-agent")
                assert session_id is not None
                assert store._current_session_id == session_id

                await store.log_event("query", "searched for something")

                stats = await store.end_session(summary="Test session")
                assert stats["session_id"] == session_id
                assert store._current_session_id is None

        finally:
            _cfg.load_config_overrides = _orig_load
            for key in (
                "MEMTOMEM_STORAGE__SQLITE_PATH",
                "MEMTOMEM_INDEXING__MEMORY_DIRS",
                "MEMTOMEM_EMBEDDING__MODEL",
                "MEMTOMEM_EMBEDDING__DIMENSION",
            ):
                os.environ.pop(key, None)
