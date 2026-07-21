"""Tests for LangGraph adapter (MemtomemStore)."""

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

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


class TestConfigOverridesStrict:
    """Unknown sections / keys in ``config_overrides`` raise ``ValueError``.

    The constructor takes a Python dict, so a typo silently falling back to
    the default DB / memory_dirs would mean writes land in the wrong place.
    We surface the error at first ``_ensure_init`` instead of warn-and-skip.
    """

    @staticmethod
    def _patch_factory(monkeypatch):
        import memtomem.config as _cfg
        import memtomem.server.component_factory as _factory

        async def _fake_create(_, **_kwargs):
            return MagicMock()

        async def _fake_close(_):
            return None

        monkeypatch.setattr(_factory, "create_components", _fake_create)
        monkeypatch.setattr(_factory, "close_components", _fake_close)
        # Block real ~/.memtomem/config.json from polluting the override chain.
        monkeypatch.setattr(_cfg, "load_config_overrides", lambda c: None)

    @pytest.mark.asyncio
    async def test_unknown_section_raises(self, monkeypatch):
        """Typo in ``config_overrides`` section name raises ValueError."""
        from memtomem.integrations.langgraph import MemtomemStore

        self._patch_factory(monkeypatch)

        store = MemtomemStore(config_overrides={"storge": {"sqlite_path": "/tmp/x.db"}})
        with pytest.raises(ValueError, match="unknown section 'storge'"):
            await store._ensure_init()

    @pytest.mark.asyncio
    async def test_unknown_key_raises(self, monkeypatch):
        """Typo in a known section's field name raises ValueError."""
        from memtomem.integrations.langgraph import MemtomemStore

        self._patch_factory(monkeypatch)

        store = MemtomemStore(config_overrides={"storage": {"sqlite_pat": "/tmp/x.db"}})
        with pytest.raises(ValueError, match="unknown key 'sqlite_pat'"):
            await store._ensure_init()

    @pytest.mark.asyncio
    async def test_non_dict_section_value_raises(self, monkeypatch):
        """A scalar where a section dict is expected also raises (catches
        e.g. ``{"storage": "/tmp/x.db"}`` from a caller skimming the docs).
        """
        from memtomem.integrations.langgraph import MemtomemStore

        self._patch_factory(monkeypatch)

        store = MemtomemStore(config_overrides={"storage": "/tmp/x.db"})
        with pytest.raises(ValueError, match="section 'storage' value is str"):
            await store._ensure_init()

    @pytest.mark.asyncio
    async def test_known_override_succeeds(self, monkeypatch):
        """Negative pin: a valid override does NOT raise (no false-positive)."""
        from memtomem.integrations.langgraph import MemtomemStore

        self._patch_factory(monkeypatch)

        store = MemtomemStore(config_overrides={"storage": {"sqlite_path": "/tmp/x.db"}})
        # Should complete without raising.
        await store._ensure_init()

    @pytest.mark.asyncio
    async def test_programmatic_override_wins_over_ambient_config(self, monkeypatch, tmp_path):
        """Constructor overrides are the final config precedence layer.

        This guards isolated integrators from silently writing to a user's
        ambient ``~/.memtomem`` database or memory directories.
        """
        import memtomem.config as _cfg
        import memtomem.server.component_factory as _factory
        from memtomem.integrations.langgraph import MemtomemStore

        isolated_db = tmp_path / "isolated.db"
        ambient_db = tmp_path / "ambient.db"
        captured = {}

        def _ambient(config):
            config.storage.sqlite_path = ambient_db

        async def _fake_create(config, *, load_ambient_config=True):
            captured["sqlite_path"] = config.storage.sqlite_path
            captured["load_ambient_config"] = load_ambient_config
            return MagicMock()

        monkeypatch.setattr(_cfg, "load_config_d", _ambient)
        monkeypatch.setattr(_cfg, "load_config_overrides", lambda config: None)
        monkeypatch.setattr(_factory, "create_components", _fake_create)

        store = MemtomemStore(config_overrides={"storage": {"sqlite_path": isolated_db}})
        await store._ensure_init()

        assert captured == {
            "sqlite_path": isolated_db,
            "load_ambient_config": False,
        }


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
    async def test_reserved_default_agent_id_binds_nothing(self):
        """#1875: ``"default"`` is the unbound sentinel on this surface too.

        Without the normalization the Python adapter would bind
        ``agent-runtime:default`` and route every subsequent ``add`` into
        a hidden system namespace — the same bug the MCP surface had,
        and the reason the fix could not stop at ``session.py``.
        """
        from memtomem.integrations.langgraph import MemtomemStore

        store = MemtomemStore()
        store._components = self._stub_components()

        sid = await store.start_agent_session("default")

        assert store._current_session_id == sid
        assert store._current_agent_id is None
        args, _ = store._components.storage.create_session.call_args
        assert args[1] == "default"  # row keeps the literal
        assert args[2] == "default"  # not agent-runtime:default
        # And the add/search resolvers therefore stay un-pinned.
        assert store._resolve_add_namespace(None) is None

    @pytest.mark.asyncio
    async def test_reserved_default_still_honors_explicit_namespace(self):
        """The ``namespace=`` escape hatch is orthogonal to the binding."""
        from memtomem.integrations.langgraph import MemtomemStore

        store = MemtomemStore()
        store._components = self._stub_components()

        await store.start_agent_session("default", namespace="custom:scope")

        args, _ = store._components.storage.create_session.call_args
        assert args[2] == "custom:scope"
        assert store._current_agent_id is None

    @pytest.mark.asyncio
    async def test_include_shared_raises_after_unbound_default_session(self):
        """Knock-on of #1875 on the Python surface, pinned deliberately.

        ``_resolve_search_namespace`` treats ``include_shared=True`` with
        no bound agent as a programming error. Before the fix
        ``start_agent_session("default")`` bound an agent, so this
        combination worked (searching ``agent-runtime:default,shared``);
        now it raises. Failing loudly is the right call — the caller
        asked for "my scope plus shared" and there is no *my scope* — but
        it is a behaviour change, so it gets a pin rather than being left
        to surface as a mystery ``ValueError`` in someone's graph.
        """
        from memtomem.integrations.langgraph import MemtomemStore

        store = MemtomemStore()
        store._components = self._stub_components()

        await store.start_agent_session("default")

        with pytest.raises(ValueError, match="requires an active agent session"):
            store._resolve_search_namespace(None, include_shared=True)

        # include_shared=False / None stay usable — they do not depend on
        # a bound agent, so the unbound session simply passes through.
        assert store._resolve_search_namespace("team", include_shared=None) == "team"

    @pytest.mark.asyncio
    async def test_none_agent_id_raises_before_storage(self):
        """``agent_id`` is required on this surface, unlike MCP.

        ``normalize_bound_agent_id`` passes ``None`` through as "nothing
        to bind" — correct where omission is the documented way to start
        an unbound session, wrong here. Without an explicit reject the
        ``None`` would reach the NOT NULL ``sessions.agent_id`` column as
        a backend ``IntegrityError`` instead of the ``InvalidNameError``
        this surface has always raised.
        """
        from memtomem.constants import InvalidNameError
        from memtomem.integrations.langgraph import MemtomemStore

        store = MemtomemStore()
        comp = self._stub_components()
        store._components = comp

        with pytest.raises(InvalidNameError):
            await store.start_agent_session(None)

        comp.storage.create_session.assert_not_awaited()
        assert store._current_session_id is None
        assert store._current_agent_id is None

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
            "blocked_files": 0,
            "blocked_paths": [],
            "errors": [],
        }

    @pytest.mark.asyncio
    async def test_index_surfaces_blocked_files_and_errors(self, tmp_path):
        """ADR-0006 PR-A gap: ``index()`` used to drop ``blocked_files`` /
        ``blocked_paths`` / ``errors`` entirely, so an agent calling this
        tool had no way to learn a secret-bearing file was skipped by the
        redaction gate. They must now round-trip into the returned dict."""
        from memtomem.integrations.langgraph import MemtomemStore
        from memtomem.models import IndexingStats

        store = MemtomemStore()

        mock_engine = MagicMock()
        mock_engine.index_path = AsyncMock(
            return_value=IndexingStats(
                total_files=2,
                total_chunks=1,
                indexed_chunks=1,
                skipped_chunks=0,
                deleted_chunks=0,
                duration_ms=42.0,
                errors=("leak.md: redaction_blocked (hits=1, scope=user, decision=blocked)",),
                blocked_files=1,
                blocked_paths=(str(tmp_path / "leak.md"),),
            )
        )
        store._components = MagicMock(index_engine=mock_engine)

        result = await store.index(path=str(tmp_path))

        assert result["blocked_files"] == 1
        assert result["blocked_paths"] == [str(tmp_path / "leak.md")]
        assert any("redaction_blocked" in e for e in result["errors"])

    @pytest.mark.asyncio
    async def test_index_engine_has_index_path(self):
        """Guards against renames of the target method on IndexEngine."""
        from memtomem.indexing.engine import IndexEngine

        assert hasattr(IndexEngine, "index_path"), (
            "IndexEngine.index_path is the target of MemtomemStore.index(); "
            "renaming it without updating the adapter will break LangGraph integration."
        )


class TestAddPrivacyGate:
    """``add()`` routes every write through ``privacy.enforce_write_guard``
    *before* any filesystem write (trust-boundary contract; the LangGraph
    adapter is one of the named ingress surfaces). A ``blocked`` decision
    must return the error dict and leave both the memory file and the
    index untouched (#1620 — the previously untested gate at
    ``integrations/langgraph.py`` ``add``).
    """

    @staticmethod
    def _guard_stub(monkeypatch, decision: str, hits: list | None = None):
        """Patch ``enforce_write_guard`` + ``append_entry``; return recorders."""
        import memtomem.privacy as privacy
        import memtomem.tools.memory_writer as memory_writer
        from memtomem.privacy import WriteGuardResult

        guard_calls: list[tuple[str, dict]] = []
        append_calls: list[tuple[tuple, dict]] = []

        def _fake_guard(content, **kwargs):
            guard_calls.append((content, kwargs))
            return WriteGuardResult(decision=decision, hits=list(hits or []))

        monkeypatch.setattr(privacy, "enforce_write_guard", _fake_guard)
        monkeypatch.setattr(
            memory_writer, "append_entry", lambda *a, **k: append_calls.append((a, k))
        )
        return guard_calls, append_calls

    @staticmethod
    def _stub_components(indexed_chunks: int = 3):
        comp = MagicMock()
        comp.index_engine.index_file = AsyncMock(
            return_value=MagicMock(indexed_chunks=indexed_chunks)
        )
        return comp

    @pytest.mark.asyncio
    async def test_blocked_decision_short_circuits_write_and_index(self, monkeypatch, tmp_path):
        from memtomem.integrations.langgraph import MemtomemStore

        guard_calls, append_calls = self._guard_stub(monkeypatch, "blocked", hits=["h1", "h2"])

        store = MemtomemStore()
        comp = self._stub_components()
        store._components = comp

        result = await store.add("secret-bearing content", file=str(tmp_path / "m.md"))

        assert result == {"error": "redaction_blocked", "hits": 2, "surface": "langgraph_add"}
        assert len(guard_calls) == 1
        assert append_calls == [], "blocked add must not write the memory file"
        comp.index_engine.index_file.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_pass_decision_writes_and_indexes_with_agent_namespace(
        self, monkeypatch, tmp_path
    ):
        from memtomem.integrations.langgraph import MemtomemStore

        guard_calls, append_calls = self._guard_stub(monkeypatch, "pass")

        store = MemtomemStore()
        comp = self._stub_components(indexed_chunks=3)
        store._components = comp
        store._current_agent_id = "planner"
        target = tmp_path / "entry.md"

        result = await store.add("safe content", title="T", tags=["t1"], file=str(target))

        # Guard saw the langgraph surface + the audit request shape.
        content, kwargs = guard_calls[0]
        assert content == "safe content"
        assert kwargs["surface"] == "langgraph_add"
        assert kwargs["force_unsafe"] is False
        assert kwargs["audit_context"] == {"namespace": None, "file": str(target)}

        # Write + index proceeded, defaulting to the bound agent's bucket.
        (args, akw) = append_calls[0]
        assert args[0] == target.resolve()
        assert args[1] == "safe content"
        assert akw == {"title": "T", "tags": ["t1"]}
        iargs, ikw = comp.index_engine.index_file.call_args
        assert iargs[0] == target.resolve()
        assert ikw["namespace"] == "agent-runtime:planner"
        assert ikw["already_scanned"] is True
        assert result == {"file": str(target.resolve()), "indexed_chunks": 3}

    @pytest.mark.asyncio
    async def test_force_unsafe_flag_reaches_guard(self, monkeypatch, tmp_path):
        from memtomem.integrations.langgraph import MemtomemStore

        guard_calls, append_calls = self._guard_stub(monkeypatch, "bypassed", hits=["h1"])

        store = MemtomemStore()
        store._components = self._stub_components()

        await store.add("content", file=str(tmp_path / "m.md"), force_unsafe=True)

        assert guard_calls[0][1]["force_unsafe"] is True
        # A bypassed (non-blocked) decision proceeds to the write.
        assert len(append_calls) == 1

    @pytest.mark.asyncio
    async def test_no_memory_dirs_errors_after_guard(self, monkeypatch):
        """Without ``file=`` and with no configured memory_dirs the call
        errors out — but only *after* the guard ran (guard-first ordering).
        """
        from memtomem.integrations.langgraph import MemtomemStore

        guard_calls, append_calls = self._guard_stub(monkeypatch, "pass")

        store = MemtomemStore()
        comp = self._stub_components()
        comp.config.indexing.memory_dirs = []
        store._components = comp

        result = await store.add("content")

        assert "indexing.memory_dirs is empty" in result["error"]
        assert len(guard_calls) == 1
        assert append_calls == []


class TestSearchDelegation:
    """``search()`` delegates to the pipeline and flattens results into
    plain dicts (the LangGraph-facing contract documented in the README
    snippet at the top of the module).
    """

    @staticmethod
    def _chunk(content: str = "hello world"):
        from memtomem.models import Chunk, ChunkMetadata

        return Chunk(
            content=content,
            metadata=ChunkMetadata(
                source_file=Path("notes/a.md"), tags=("t1",), namespace="default"
            ),
        )

    @staticmethod
    def _store_with_pipeline(results):
        from memtomem.integrations.langgraph import MemtomemStore

        pipeline = MagicMock()
        pipeline.search = AsyncMock(return_value=(results, MagicMock()))
        store = MemtomemStore()
        store._components = MagicMock(search_pipeline=pipeline)
        return store, pipeline

    @pytest.mark.asyncio
    async def test_maps_pipeline_results_to_dicts(self, monkeypatch):
        import memtomem.server.tools.search as search_tools

        monkeypatch.setattr(search_tools, "_resolve_project_context_root", lambda comp: None)

        chunk = self._chunk()
        store, pipeline = self._store_with_pipeline(
            [SimpleNamespace(chunk=chunk, score=0.9, rank=1)]
        )

        results = await store.search("hello", top_k=3)

        assert results == [
            {
                "id": str(chunk.id),
                "content": "hello world",
                "score": 0.9,
                # str(Path) — platform separator (Windows: notes\a.md)
                "source": str(Path("notes/a.md")),
                "tags": ["t1"],
                "namespace": "default",
                "rank": 1,
            }
        ]
        kwargs = pipeline.search.call_args.kwargs
        assert kwargs["query"] == "hello"
        assert kwargs["top_k"] == 3
        assert kwargs["rrf_weights"] is None
        assert kwargs["namespace"] is None

    @pytest.mark.asyncio
    async def test_partial_weights_agent_namespace_and_project_root_threaded(self, monkeypatch):
        """A single explicit weight fills the other side with 1.0; a bound
        agent pins the namespace; the resolved project context root is
        threaded through to the pipeline (ADR-0011 PR-D round 9).
        """
        import memtomem.server.tools.search as search_tools

        monkeypatch.setattr(
            search_tools, "_resolve_project_context_root", lambda comp: "/proj/root"
        )

        store, pipeline = self._store_with_pipeline([])
        store._current_agent_id = "planner"

        results = await store.search("q", bm25_weight=0.7)

        assert results == []
        kwargs = pipeline.search.call_args.kwargs
        assert kwargs["rrf_weights"] == [0.7, 1.0]
        assert kwargs["namespace"] == "agent-runtime:planner,shared"
        assert kwargs["project_context_root"] == "/proj/root"


class TestGetDelete:
    @pytest.mark.asyncio
    async def test_get_maps_chunk_to_dict(self):
        from memtomem.integrations.langgraph import MemtomemStore
        from memtomem.models import Chunk, ChunkMetadata

        chunk = Chunk(
            content="c",
            metadata=ChunkMetadata(source_file=Path("n/a.md"), tags=("x",), namespace="ns"),
        )
        comp = MagicMock()
        comp.storage.get_chunk = AsyncMock(return_value=chunk)
        store = MemtomemStore()
        store._components = comp

        got = await store.get(str(chunk.id))

        assert got == {
            "id": str(chunk.id),
            "content": "c",
            # str(Path) — platform separator (Windows: n\a.md)
            "source": str(Path("n/a.md")),
            "tags": ["x"],
            "namespace": "ns",
        }
        comp.storage.get_chunk.assert_awaited_once_with(chunk.id)

    @pytest.mark.asyncio
    async def test_get_missing_returns_none(self):
        from memtomem.integrations.langgraph import MemtomemStore

        comp = MagicMock()
        comp.storage.get_chunk = AsyncMock(return_value=None)
        store = MemtomemStore()
        store._components = comp

        assert await store.get(str(uuid4())) is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("deleted_rows", "expected"), [(1, True), (0, False)])
    async def test_delete_reports_whether_rows_were_deleted(self, deleted_rows, expected):
        from memtomem.integrations.langgraph import MemtomemStore

        cid = uuid4()
        comp = MagicMock()
        comp.storage.delete_chunks = AsyncMock(return_value=deleted_rows)
        store = MemtomemStore()
        store._components = comp

        assert await store.delete(str(cid)) is expected
        comp.storage.delete_chunks.assert_awaited_once_with([cid])


class TestStartSessionLowLevel:
    """``start_session`` is the low-level escape hatch: it records the
    session but must NOT bind ``_current_agent_id`` (that's
    ``start_agent_session``'s contract), and an explicit ``namespace``
    override is gated by ``validate_namespace`` (issue #496).
    """

    @staticmethod
    def _stub_components():
        comp = MagicMock()
        comp.storage.create_session = AsyncMock(return_value=None)
        return comp

    @pytest.mark.asyncio
    async def test_defaults_namespace_and_does_not_bind_agent(self):
        from memtomem.integrations.langgraph import MemtomemStore

        store = MemtomemStore()
        comp = self._stub_components()
        store._components = comp

        sid = await store.start_session()

        args, _ = comp.storage.create_session.call_args
        assert args == (sid, "default", "default")
        assert store._current_session_id == sid
        assert store._current_agent_id is None

    @pytest.mark.asyncio
    async def test_explicit_valid_namespace_stored_verbatim(self):
        from memtomem.integrations.langgraph import MemtomemStore

        store = MemtomemStore()
        comp = self._stub_components()
        store._components = comp

        await store.start_session(agent_id="a1", namespace="custom.scope")

        args, _ = comp.storage.create_session.call_args
        assert args[1] == "a1"
        assert args[2] == "custom.scope"

    @pytest.mark.asyncio
    async def test_replacing_agent_session_clears_stale_agent_binding(self):
        """Starting a low-level session after ``start_agent_session`` must
        drop the previous agent binding — otherwise subsequent ``add`` /
        ``search`` calls keep defaulting to the old ``agent-runtime:<id>``
        scope while events log to the new session (stale-binding bug
        caught in #1620's review).
        """
        from memtomem.integrations.langgraph import MemtomemStore

        store = MemtomemStore()
        store._components = self._stub_components()

        await store.start_agent_session("planner")
        assert store._current_agent_id == "planner"

        sid = await store.start_session()

        assert store._current_session_id == sid
        assert store._current_agent_id is None
        assert store._resolve_add_namespace(None) is None

    @pytest.mark.asyncio
    async def test_malformed_namespace_blocked_before_storage(self):
        """Regression pin (#496): the Python adapter's low-level entry point
        must refuse ``"agent-runtime:foo:bar"`` just like
        ``start_agent_session`` does — otherwise this path reintroduces
        the namespace-smuggling gap the gate was built to close.
        """
        from memtomem.constants import InvalidNameError
        from memtomem.integrations.langgraph import MemtomemStore

        store = MemtomemStore()
        comp = self._stub_components()
        store._components = comp

        with pytest.raises(InvalidNameError):
            await store.start_session(namespace="agent-runtime:foo:bar")

        comp.storage.create_session.assert_not_awaited()
        assert store._current_session_id is None


class TestEndSessionAggregation:
    @pytest.mark.asyncio
    async def test_no_active_session_returns_error(self):
        from memtomem.integrations.langgraph import MemtomemStore

        store = MemtomemStore()
        store._components = MagicMock()

        assert await store.end_session() == {"error": "no active session"}

    @pytest.mark.asyncio
    async def test_aggregates_event_counts_and_cleans_up(self):
        from memtomem.integrations.langgraph import MemtomemStore

        events = [
            {"event_type": "query"},
            {"event_type": "query"},
            {"event_type": "write"},
        ]
        comp = MagicMock()
        comp.storage.get_session_events = AsyncMock(return_value=events)
        comp.storage.end_session = AsyncMock(return_value=None)
        comp.storage.scratch_cleanup = AsyncMock(return_value=0)

        store = MemtomemStore()
        store._components = comp
        store._current_session_id = "sess-9"

        result = await store.end_session(summary="done")

        comp.storage.end_session.assert_awaited_once_with(
            "sess-9", "done", {"event_counts": {"query": 2, "write": 1}}
        )
        comp.storage.scratch_cleanup.assert_awaited_once_with(session_id="sess-9")
        assert result == {
            "session_id": "sess-9",
            "events": 3,
            "event_counts": {"query": 2, "write": 1},
        }


class TestLogEvent:
    @pytest.mark.asyncio
    async def test_no_session_is_a_noop_before_init(self):
        """Without a session ``log_event`` returns before ``_ensure_init``
        — components must stay untouched (still ``None``).
        """
        from memtomem.integrations.langgraph import MemtomemStore

        store = MemtomemStore()

        await store.log_event("query", "content")

        assert store._components is None

    @pytest.mark.asyncio
    async def test_delegates_to_storage_with_session_id(self):
        from memtomem.integrations.langgraph import MemtomemStore

        comp = MagicMock()
        comp.storage.add_session_event = AsyncMock(return_value=None)
        store = MemtomemStore()
        store._components = comp
        store._current_session_id = "sess-1"

        await store.log_event("query", "looked up X", chunk_ids=["c1"])

        comp.storage.add_session_event.assert_awaited_once_with(
            "sess-1", "query", "looked up X", ["c1"]
        )


class TestScratchDelegation:
    @staticmethod
    def _store_with_storage():
        from memtomem.integrations.langgraph import MemtomemStore

        comp = MagicMock()
        comp.storage.scratch_set = AsyncMock(return_value=None)
        comp.storage.scratch_get = AsyncMock(return_value=None)
        comp.storage.scratch_list = AsyncMock(return_value=[])
        store = MemtomemStore()
        store._components = comp
        return store, comp

    @pytest.mark.asyncio
    async def test_scratch_set_without_ttl_has_no_expiry(self):
        store, comp = self._store_with_storage()
        store._current_session_id = "s1"

        await store.scratch_set("k", "v")

        comp.storage.scratch_set.assert_awaited_once_with(
            "k", "v", session_id="s1", expires_at=None
        )

    @pytest.mark.asyncio
    async def test_scratch_set_with_ttl_computes_iso_expiry(self):
        store, comp = self._store_with_storage()

        await store.scratch_set("k", "v", ttl_minutes=5)

        expires_at = comp.storage.scratch_set.call_args.kwargs["expires_at"]
        assert expires_at is not None
        # ISO-8601 with seconds precision — must round-trip through fromisoformat.
        datetime.fromisoformat(expires_at)

    @pytest.mark.asyncio
    async def test_scratch_get_unwraps_value(self):
        store, comp = self._store_with_storage()
        comp.storage.scratch_get = AsyncMock(return_value={"value": "v"})

        assert await store.scratch_get("k") == "v"

    @pytest.mark.asyncio
    async def test_scratch_get_missing_returns_none(self):
        store, _ = self._store_with_storage()

        assert await store.scratch_get("missing") is None

    @pytest.mark.asyncio
    async def test_scratch_list_scoped_to_current_session(self):
        store, comp = self._store_with_storage()
        comp.storage.scratch_list = AsyncMock(return_value=[{"key": "k"}])
        store._current_session_id = "s1"

        assert await store.scratch_list() == [{"key": "k"}]
        comp.storage.scratch_list.assert_awaited_once_with(session_id="s1")


class TestCloseAndContextManager:
    @pytest.mark.asyncio
    async def test_close_releases_components(self, monkeypatch):
        import memtomem.server.component_factory as factory
        from memtomem.integrations.langgraph import MemtomemStore

        closed = []

        async def _fake_close(comp):
            closed.append(comp)

        monkeypatch.setattr(factory, "close_components", _fake_close)

        store = MemtomemStore()
        comp = MagicMock()
        store._components = comp

        await store.close()

        assert closed == [comp]
        assert store._components is None

    @pytest.mark.asyncio
    async def test_close_before_init_is_a_noop(self):
        from memtomem.integrations.langgraph import MemtomemStore

        store = MemtomemStore()
        await store.close()  # must not raise or import the factory
        assert store._components is None

    @pytest.mark.asyncio
    async def test_context_manager_inits_on_enter_and_closes_on_exit(self, monkeypatch):
        import memtomem.config as _cfg
        import memtomem.server.component_factory as factory
        from memtomem.integrations.langgraph import MemtomemStore

        comp = MagicMock()
        closed = []

        async def _fake_create(_config, **_kwargs):
            return comp

        async def _fake_close(c):
            closed.append(c)

        monkeypatch.setattr(factory, "create_components", _fake_create)
        monkeypatch.setattr(factory, "close_components", _fake_close)
        # Block real ~/.memtomem/config.json from polluting the override chain.
        monkeypatch.setattr(_cfg, "load_config_overrides", lambda c: None)

        async with MemtomemStore() as store:
            assert store._components is comp

        assert closed == [comp]
        assert store._components is None


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
