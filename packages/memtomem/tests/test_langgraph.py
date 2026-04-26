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

    def test_agent_id_none_initially(self):
        """``_current_agent_id`` is set by ``start_agent_session`` only —
        a fresh ``MemtomemStore`` reports no bound agent.
        """
        from memtomem.integrations.langgraph import MemtomemStore

        store = MemtomemStore()
        assert store._current_agent_id is None


class TestResolveSearchNamespace:
    """``_resolve_search_namespace`` encodes the 6-case ``include_shared``
    table documented in ``MemtomemStore.search``. Drift here would let the
    "include the shared slice of an agent's view" promise degrade to a
    silent un-pinned search — exactly the kind of fallback the multi-agent
    plan calls out.
    """

    def _store_with_agent(self, agent_id: str | None):
        from memtomem.integrations.langgraph import MemtomemStore

        store = MemtomemStore()
        store._current_agent_id = agent_id
        return store

    def test_auto_with_agent_includes_shared(self):
        store = self._store_with_agent("planner")
        assert (
            store._resolve_search_namespace(namespace=None, include_shared=None)
            == "agent-runtime:planner,shared"
        )

    def test_auto_without_agent_defers_to_caller_namespace(self):
        store = self._store_with_agent(None)
        assert store._resolve_search_namespace(namespace="archive:old", include_shared=None) == (
            "archive:old"
        )

    def test_auto_without_agent_and_no_namespace_returns_none(self):
        store = self._store_with_agent(None)
        assert store._resolve_search_namespace(namespace=None, include_shared=None) is None

    def test_explicit_true_with_agent_includes_shared(self):
        store = self._store_with_agent("planner")
        assert (
            store._resolve_search_namespace(namespace=None, include_shared=True)
            == "agent-runtime:planner,shared"
        )

    def test_explicit_true_without_agent_raises(self):
        """Surface programming bugs immediately — silent fallback would let
        a multi-agent caller leak into an un-pinned search.
        """
        store = self._store_with_agent(None)
        with pytest.raises(ValueError, match="active agent session"):
            store._resolve_search_namespace(namespace=None, include_shared=True)

    def test_explicit_false_with_agent_excludes_shared(self):
        store = self._store_with_agent("planner")
        assert (
            store._resolve_search_namespace(namespace=None, include_shared=False)
            == "agent-runtime:planner"
        )

    def test_explicit_false_without_agent_passes_caller_namespace(self):
        store = self._store_with_agent(None)
        assert (
            store._resolve_search_namespace(namespace="legacy:ns", include_shared=False)
            == "legacy:ns"
        )


class TestResolveAddNamespace:
    """``_resolve_add_namespace`` defaults to the bound agent's private
    bucket when the caller omits ``namespace``. An explicit ``namespace=``
    always wins so an agent can opt-in to writing to ``shared`` mid-session.
    """

    def _store_with_agent(self, agent_id: str | None):
        from memtomem.integrations.langgraph import MemtomemStore

        store = MemtomemStore()
        store._current_agent_id = agent_id
        return store

    def test_no_agent_no_namespace_returns_none(self):
        store = self._store_with_agent(None)
        assert store._resolve_add_namespace(None) is None

    def test_no_agent_with_namespace_returns_namespace(self):
        store = self._store_with_agent(None)
        assert store._resolve_add_namespace("custom:ns") == "custom:ns"

    def test_agent_no_namespace_defaults_to_agent_runtime(self):
        store = self._store_with_agent("planner")
        assert store._resolve_add_namespace(None) == "agent-runtime:planner"

    def test_agent_with_explicit_namespace_wins(self):
        """Explicit ``namespace="shared"`` lets a planner-bound session
        publish into the shared bucket without re-binding the session.
        """
        store = self._store_with_agent("planner")
        assert store._resolve_add_namespace("shared") == "shared"


class TestStartAgentSession:
    """``start_agent_session`` derives the namespace from the agent id and
    binds ``_current_agent_id``. Uses an injected ``_components`` mock so
    tests do not need to spin up storage / embedder.
    """

    def _stub_components(self):
        comp = MagicMock()
        comp.storage.create_session = AsyncMock(return_value=None)
        return comp

    @pytest.mark.asyncio
    async def test_binds_agent_id_and_derives_namespace(self):
        from memtomem.integrations.langgraph import MemtomemStore

        store = MemtomemStore()
        store._components = self._stub_components()

        sid = await store.start_agent_session("planner")

        assert sid is not None
        assert store._current_session_id == sid
        assert store._current_agent_id == "planner"
        # storage.create_session was called with the derived agent-runtime: namespace
        args, _ = store._components.storage.create_session.call_args
        assert args[1] == "planner"  # agent_id
        assert args[2] == "agent-runtime:planner"  # namespace

    @pytest.mark.asyncio
    async def test_explicit_namespace_overrides_default(self):
        from memtomem.integrations.langgraph import MemtomemStore

        store = MemtomemStore()
        store._components = self._stub_components()

        await store.start_agent_session("planner", namespace="custom:scope")

        args, _ = store._components.storage.create_session.call_args
        assert args[2] == "custom:scope"
        # Agent binding still happens — caller wanted a custom namespace,
        # not to skip the multi-agent semantic.
        assert store._current_agent_id == "planner"

    @pytest.mark.asyncio
    async def test_empty_agent_id_raises(self):
        from memtomem.constants import InvalidNameError
        from memtomem.integrations.langgraph import MemtomemStore

        store = MemtomemStore()
        comp = self._stub_components()
        store._components = comp

        with pytest.raises(InvalidNameError, match="invalid agent-id"):
            await store.start_agent_session("")
        comp.storage.create_session.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "agent_id",
        [
            "foo:bar",  # collides with the namespace separator
            "../etc",  # path traversal
            "a/b",  # path separator
            "a b",  # internal whitespace
            "-leading-dash",
        ],
    )
    async def test_hostile_agent_id_blocked_before_storage(self, agent_id):
        """Regression pin (#492 / PR #491 follow-up): the LangGraph adapter
        must apply the same ``validate_agent_id`` gate as the MCP / CLI
        surfaces, so a malformed namespace like ``"agent-runtime:foo:bar"``
        cannot reach storage from the in-process Python entry point.
        """
        from memtomem.constants import InvalidNameError
        from memtomem.integrations.langgraph import MemtomemStore

        store = MemtomemStore()
        comp = self._stub_components()
        store._components = comp

        with pytest.raises(InvalidNameError, match="invalid agent-id"):
            await store.start_agent_session(agent_id)

        comp.storage.create_session.assert_not_awaited()
        # Binding state stays clean — a rejected start_agent_session
        # must not leave _current_agent_id pointing at the hostile value.
        assert store._current_session_id is None
        assert store._current_agent_id is None

    @pytest.mark.asyncio
    async def test_end_session_resets_agent_id(self):
        from memtomem.integrations.langgraph import MemtomemStore

        store = MemtomemStore()
        comp = self._stub_components()
        comp.storage.get_session_events = AsyncMock(return_value=[])
        comp.storage.end_session = AsyncMock(return_value=None)
        comp.storage.scratch_cleanup = AsyncMock(return_value=0)
        store._components = comp

        await store.start_agent_session("planner")
        assert store._current_agent_id == "planner"

        await store.end_session(summary="done")
        assert store._current_session_id is None
        assert store._current_agent_id is None


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
