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
        import memtomem.runtime.components as _factory

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
        import memtomem.runtime.components as _factory
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
            "retryable_errors": [],
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
    async def test_index_surfaces_retryable_error_subset(self, tmp_path):
        from memtomem.integrations.langgraph import MemtomemStore
        from memtomem.models import IndexingStats

        permanent = "broken.md: malformed frontmatter"
        retryable = "transient.md: chunk store unavailable"
        store = MemtomemStore()
        mock_engine = MagicMock()
        mock_engine.index_path = AsyncMock(
            return_value=IndexingStats(
                total_files=2,
                total_chunks=0,
                indexed_chunks=0,
                skipped_chunks=0,
                deleted_chunks=0,
                duration_ms=1.0,
                errors=(permanent, retryable),
                retryable_errors=(retryable,),
            )
        )
        store._components = MagicMock(index_engine=mock_engine)

        result = await store.index(path=str(tmp_path))

        assert result["errors"] == [permanent, retryable]
        assert result["retryable_errors"] == [retryable]

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
        # ``add`` classifies its target against this list (ADR-0011 §5, #2321).
        # Named explicitly rather than left as a MagicMock attribute so these
        # tests assert a user-tier write because the config says so, not
        # because an auto-created mock happened to iterate empty.
        comp.config.indexing.project_memory_dirs = []
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
        assert kwargs["audit_context"] == {
            "namespace": None,
            "file": str(target),
            "scope": "user",
        }
        # The guard is told which tier it is scanning for (#2321). Before that
        # kwarg existed the scan always claimed ``user``, so Gate A's
        # project_shared hard-refusal could not fire from this surface.
        assert kwargs["scope"] == "user"

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
    async def test_no_memory_dirs_errors_before_guard(self, monkeypatch):
        """Without ``file=`` and with no configured memory_dirs the call
        errors out **before** the guard runs.

        This inverts the guard-first ordering the test pinned until #2321,
        and the inversion is the fix rather than a casualty of it: the scan
        cannot be told which tier it is scanning for until the destination
        has been chosen, so target resolution now precedes it. Nothing is
        lost by scanning later — no bytes reach the filesystem either way,
        and the write itself is still strictly after the guard (pinned by
        ``test_blocked_decision_short_circuits_write_and_index`` and by the
        call-order test in ``test_redaction_write_surfaces.py``).
        """
        from memtomem.integrations.langgraph import MemtomemStore

        guard_calls, append_calls = self._guard_stub(monkeypatch, "pass")

        store = MemtomemStore()
        comp = self._stub_components()
        comp.config.indexing.memory_dirs = []
        store._components = comp

        result = await store.add("content")

        assert "indexing.memory_dirs is empty" in result["error"]
        assert guard_calls == [], "target resolution failed, so nothing was scanned"
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
    async def test_a_negative_weight_is_refused_before_initialization(self):
        """The refusal must not sit behind ``_ensure_init``.

        Initialization can fail for its own reasons; if it ran first, the
        actionable "your weight is invalid" error would be replaced by
        whatever that failure raised.
        """
        from memtomem.integrations.langgraph import MemtomemStore
        from memtomem.services.search_service import InvalidRrfWeightError

        store = MemtomemStore()
        store._ensure_init = AsyncMock(side_effect=AssertionError("initialized too early"))

        with pytest.raises(InvalidRrfWeightError, match="bm25_weight"):
            await store.search("hello", bm25_weight=-1.0)

    @pytest.mark.asyncio
    async def test_an_all_zero_weight_pair_is_refused_before_initialization(self):
        from memtomem.integrations.langgraph import MemtomemStore
        from memtomem.services.search_service import InvalidRrfWeightError

        store = MemtomemStore()
        store._ensure_init = AsyncMock(side_effect=AssertionError("initialized too early"))

        with pytest.raises(InvalidRrfWeightError, match="cannot both be zero"):
            await store.search("hello", bm25_weight=0.0, dense_weight=0.0)

    @pytest.mark.asyncio
    async def test_a_zero_weight_reaches_the_pipeline_unchanged(self, monkeypatch):
        """#2087: the adapter carried the same ``or 1.0`` truthiness bug.

        A caller asking to drop the keyword leg's score contribution with
        ``bm25_weight=0.0`` got ``1.0`` — the opposite — and no error to
        notice it by.
        """
        import memtomem.runtime.project_context as project_context

        monkeypatch.setattr(project_context, "_resolve_project_context_root", lambda comp: None)

        store, pipeline = self._store_with_pipeline([])

        await store.search("hello", bm25_weight=0.0)

        assert pipeline.search.await_args.kwargs["rrf_weights"] == [0.0, 1.0]

    @pytest.mark.asyncio
    async def test_absent_weights_defer_to_server_config(self, monkeypatch):
        import memtomem.runtime.project_context as project_context

        monkeypatch.setattr(project_context, "_resolve_project_context_root", lambda comp: None)

        store, pipeline = self._store_with_pipeline([])

        await store.search("hello")

        assert pipeline.search.await_args.kwargs["rrf_weights"] is None

    @pytest.mark.asyncio
    async def test_maps_pipeline_results_to_dicts(self, monkeypatch):
        import memtomem.runtime.project_context as project_context

        monkeypatch.setattr(project_context, "_resolve_project_context_root", lambda comp: None)

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
        assert kwargs["origin"] == "langgraph"

    @pytest.mark.asyncio
    async def test_partial_weights_agent_namespace_and_project_root_threaded(self, monkeypatch):
        """A single explicit weight fills the other side with 1.0; a bound
        agent pins the namespace; the resolved project context root is
        threaded through to the pipeline (ADR-0011 PR-D round 9).
        """
        import memtomem.runtime.project_context as project_context

        monkeypatch.setattr(
            project_context, "_resolve_project_context_root", lambda comp: "/proj/root"
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

        from memtomem.models import Chunk, ChunkMetadata

        cid = uuid4()
        comp = MagicMock()
        # Delete screens the chunk first (ADR-0036), so the row has to exist
        # and be in-boundary before ``delete_chunks`` is reached at all.
        comp.storage.get_chunk = AsyncMock(
            return_value=Chunk(
                content="c",
                metadata=ChunkMetadata(source_file=Path("n/a.md")),
                id=cid,
            )
        )
        comp.storage.delete_chunks = AsyncMock(return_value=deleted_rows)
        store = MemtomemStore()
        store._components = comp

        assert await store.delete(str(cid)) is expected
        comp.storage.delete_chunks.assert_awaited_once_with([cid])

    @pytest.mark.asyncio
    async def test_delete_refuses_an_out_of_boundary_id(self):
        """ADR-0036: what cannot be read here cannot be deleted here either.

        ``False`` is the same answer a nonexistent id gets, and
        ``delete_chunks`` is never reached.
        """
        from memtomem.integrations.langgraph import MemtomemStore
        from memtomem.models import Chunk, ChunkMetadata

        cid = uuid4()
        comp = MagicMock()
        comp.config.indexing.project_memory_dirs = []
        comp.storage.get_chunk = AsyncMock(
            return_value=Chunk(
                content="another project's note",
                metadata=ChunkMetadata(
                    source_file=Path("/elsewhere/.memtomem/memories/n.md"),
                    scope="project_shared",
                    project_root=Path("/elsewhere"),
                ),
                id=cid,
            )
        )
        comp.storage.delete_chunks = AsyncMock(return_value=1)
        store = MemtomemStore()
        store._components = comp

        assert await store.delete(str(cid)) is False
        assert await store.get(str(cid)) is None
        comp.storage.delete_chunks.assert_not_awaited()


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
            "sess-9",
            "done",
            {"event_counts": {"query": 2, "write": 1}, "summary_provenance": "manual"},
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
        import memtomem.runtime.components as factory
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
        import memtomem.runtime.components as factory
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


class TestLangGraphEndSessionProvenance:
    """``MemtomemStore.end_session`` stamps ``summary_provenance='manual'``
    (a caller-supplied summary, not the server's write-provenance selection)
    only when a summary is passed; ending with none leaves it absent (#1913).
    """

    @staticmethod
    def _store_with_end_spy():
        from memtomem.integrations.langgraph import MemtomemStore

        store = MemtomemStore()
        comp = MagicMock()
        comp.storage.end_session = AsyncMock(return_value=None)
        comp.storage.get_session_events = AsyncMock(return_value=[])
        comp.storage.scratch_cleanup = AsyncMock(return_value=0)
        store._components = comp
        store._current_session_id = "sess-lg"
        store._current_agent_id = "planner"
        return store, comp.storage.end_session

    @pytest.mark.asyncio
    async def test_summary_records_manual(self):
        store, end_session = self._store_with_end_spy()

        await store.end_session(summary="run notes")

        assert end_session.await_args.args[2]["summary_provenance"] == "manual"

    @pytest.mark.asyncio
    async def test_no_summary_records_no_provenance(self):
        store, end_session = self._store_with_end_spy()

        await store.end_session()

        assert "summary_provenance" not in end_session.await_args.args[2]


class TestAddProjectSharedGateB:
    """ADR-0011 §5 on the LangGraph adapter's ``add`` (#2321).

    ``MemtomemStore.add`` takes a caller-supplied ``file=``, which makes it
    the one path on this adapter that can choose the git-tracked tier. Until
    #2321 it ran the redaction scan twelve lines before the target was
    resolved and never passed ``scope=``, so Gate A always scanned as
    ``user`` and Gate B did not exist at all.

    These are the behavioural half the AST guard in
    ``test_project_shared_confirmation_audit_guard.py`` cannot cover: that
    the consent emit's predicate mirrors the gate's, that the surface name
    is this adapter's own, and that Gate A is handed the tier it is
    scanning for.

    Built on real components rather than mocks, because the property under
    test is what ``classify_scope`` answers for a real registered
    ``project_memory_dirs`` — a mocked config would let the tests agree with
    themselves.
    """

    _CLEAN = "Met with the team about the Q2 deploy plan."
    _SECRET = "Notes on token: sk-" + "a" * 30

    @pytest.fixture
    def tiers(self, tmp_path, monkeypatch):
        """A store whose three tiers are all real, registered directories.

        Returns ``(store, dirs)`` where ``dirs`` maps a tier name to the
        directory ``add(file=...)`` should be pointed at. ``unregistered``
        is project-canonical by *path shape* but named by no config field —
        the case ``classify_scope`` answers ``user`` for.
        """
        from helpers import isolate_memtomem_env
        from memtomem.integrations.langgraph import MemtomemStore

        isolate_memtomem_env(monkeypatch)

        user_dir = tmp_path / "user_mem"
        shared_dir = tmp_path / "proj" / ".memtomem" / "memories"
        local_dir = tmp_path / "proj" / ".memtomem" / "memories.local"
        unregistered_dir = tmp_path / "other_proj" / ".memtomem" / "memories"
        for d in (user_dir, shared_dir, local_dir, unregistered_dir):
            d.mkdir(parents=True)

        store = MemtomemStore(
            config_overrides={
                "storage": {"sqlite_path": tmp_path / "lg.db"},
                "indexing": {
                    "memory_dirs": [user_dir],
                    "project_memory_dirs": [shared_dir, local_dir],
                },
                "embedding": {"dimension": 1024},
                "search": {"enable_dense": False},
            }
        )
        yield (
            store,
            {
                "user": user_dir,
                "project_shared": shared_dir,
                "project_local": local_dir,
                "unregistered": unregistered_dir,
            },
        )

    @staticmethod
    def _seed(target: Path) -> str:
        """Write a known body so "the file did not change" is not vacuous."""
        marker = "## Seeded\n\nprior body\n"
        target.write_text(marker, encoding="utf-8")
        return marker

    @pytest.mark.asyncio
    async def test_shared_target_without_confirm_refuses_and_leaves_the_file_alone(
        self, tiers, caplog
    ):
        import logging

        from helpers import consent_lines

        store, dirs = tiers
        target = dirs["project_shared"] / "notes.md"
        seeded = self._seed(target)

        try:
            with caplog.at_level(logging.WARNING, logger="memtomem.privacy"):
                result = await store.add(self._CLEAN, file=str(target))
        finally:
            await store.close()

        assert result["error"] == "project_shared_confirmation_required"
        assert "confirm_project_shared=True" in result["detail"]
        assert target.read_text(encoding="utf-8") == seeded
        # No consent was given, so none may be recorded.
        assert consent_lines(caplog) == []

    @pytest.mark.asyncio
    async def test_shared_target_with_confirm_writes_and_records_one_consent(self, tiers, caplog):
        import logging

        from helpers import consent_lines

        store, dirs = tiers
        target = dirs["project_shared"] / "notes.md"
        seeded = self._seed(target)

        try:
            comp = await store._ensure_init()
            with caplog.at_level(logging.WARNING, logger="memtomem.privacy"):
                result = await store.add(self._CLEAN, file=str(target), confirm_project_shared=True)

            assert result.get("error") is None, result
            assert result["file"] == str(target)
            assert self._CLEAN in target.read_text(encoding="utf-8")
            assert target.read_text(encoding="utf-8") != seeded

            # Exactly one line, naming this surface. A second would mean the
            # consent is recorded somewhere else too and the audit over-counts
            # one human decision.
            lines = consent_lines(caplog)
            assert len(lines) == 1, lines
            assert "project_shared.confirmed_via=langgraph_add" in lines[0]
            assert "mechanism=param" in lines[0]
            assert "action=write" in lines[0]

            # The tier the gates adjudicated is the tier the row ends up in.
            # Bytes and a log line alone would let the two gates agree with
            # each other and still disagree with the indexer, which is the
            # state #2321 is about.
            chunks = await comp.storage.list_chunks_by_source(target)
            assert chunks, "the write should have produced at least one chunk"
            for chunk in chunks:
                assert chunk.metadata.scope == "project_shared", chunk.metadata.scope
                assert chunk.metadata.project_root is not None
        finally:
            await store.close()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("tier", ["user", "project_local"])
    @pytest.mark.parametrize("confirmed", [False, True])
    async def test_other_tiers_write_without_asking_and_record_no_consent(
        self, tiers, caplog, tier, confirmed
    ):
        """Neither axis may leak into the other.

        The ``project_local`` tier catches a gate widened from
        ``== "project_shared"`` to ``!= "user"`` — ``memories.local`` is
        gitignored and was never meant to ask. The ``confirmed=True`` axis
        catches an emit that fires on the flag rather than on the tier,
        filing a consent for a destination nobody was asked about.
        """
        import logging

        from helpers import consent_lines

        store, dirs = tiers
        target = dirs[tier] / "notes.md"

        try:
            with caplog.at_level(logging.WARNING, logger="memtomem.privacy"):
                result = await store.add(
                    self._CLEAN, file=str(target), confirm_project_shared=confirmed
                )
        finally:
            await store.close()

        assert result.get("error") is None, result
        assert self._CLEAN in target.read_text(encoding="utf-8")
        assert consent_lines(caplog) == []

    @pytest.mark.asyncio
    async def test_gate_a_hard_refuses_force_unsafe_into_the_shared_tier(self, tiers, caplog):
        """The bug this closes, end to end.

        Before #2321 the guard was called without ``scope=``, so a secret
        plus ``force_unsafe=True`` plus a project-canonical ``file=`` was
        recorded as an ordinary ``bypassed`` and written into a
        repository-tracked file. Now it is ``blocked_project_shared``, and
        nothing lands.

        The surviving consent line is the ordering pin: it records that
        consent was *given*, not that the write landed, so moving the emit
        behind Gate A would drop it and leave every other assertion green.
        """
        import logging

        from memtomem import privacy

        from helpers import consent_lines

        store, dirs = tiers
        target = dirs["project_shared"] / "notes.md"
        seeded = self._seed(target)

        before = privacy.snapshot()["by_tool"].get("langgraph_add", {})

        try:
            with caplog.at_level(logging.WARNING, logger="memtomem.privacy"):
                result = await store.add(
                    self._SECRET,
                    file=str(target),
                    force_unsafe=True,
                    confirm_project_shared=True,
                )
        finally:
            await store.close()

        after = privacy.snapshot()["by_tool"]["langgraph_add"]

        assert result["error"] == "redaction_blocked_project_shared"
        assert result["surface"] == "langgraph_add"
        assert result["hits"] >= 1
        assert target.read_text(encoding="utf-8") == seeded
        assert after.get("blocked_project_shared", 0) == before.get("blocked_project_shared", 0) + 1
        # The valve did not open: this must not be filed as an ordinary bypass.
        assert after.get("bypassed", 0) == before.get("bypassed", 0)
        assert len(consent_lines(caplog)) == 1

    @pytest.mark.asyncio
    async def test_unregistered_project_target_is_refused(self, tiers, caplog):
        """A project-canonical path nobody registered is refused, not re-tiered.

        ``classify_scope`` answers ``user`` for it. Taking that answer would
        stamp user-tier rows onto a git-tracked path and route the write
        past both gates — the quiet version of the same bug.
        """
        import logging

        from helpers import consent_lines

        store, dirs = tiers
        target = dirs["unregistered"] / "notes.md"

        try:
            with caplog.at_level(logging.WARNING, logger="memtomem.privacy"):
                result = await store.add(self._CLEAN, file=str(target), confirm_project_shared=True)
        finally:
            await store.close()

        assert result["error"] == "unregistered_project_target"
        assert not target.exists(), "a refused target must not be created"
        assert consent_lines(caplog) == []

    @pytest.mark.asyncio
    async def test_gate_b_answers_before_gate_a_scans(self, tiers, caplog):
        """Which refusal a caller gets when both gates would fire.

        Gate B first: a caller who never consented to a git-tracked write
        should be told that, not that their content looked like a secret —
        and the scan should not have run at all, so no scan outcome is
        recorded for the call.
        """
        import logging

        from memtomem import privacy

        from helpers import consent_lines

        store, dirs = tiers
        target = dirs["project_shared"] / "notes.md"
        self._seed(target)

        before = privacy.snapshot()["by_tool"].get("langgraph_add", {})

        try:
            with caplog.at_level(logging.WARNING, logger="memtomem.privacy"):
                result = await store.add(self._SECRET, file=str(target))
        finally:
            await store.close()

        after = privacy.snapshot()["by_tool"].get("langgraph_add", {})

        assert result["error"] == "project_shared_confirmation_required"
        assert after == before, "Gate B refused, so no content scan should be recorded"
        assert consent_lines(caplog) == []

    @pytest.mark.asyncio
    async def test_default_user_tier_add_is_untouched_by_the_reorder(self, tiers, caplog):
        """The regression floor: no ``file=``, no gates, same result shape.

        Every assertion above is about the ``file=`` branch. This one exists
        so a reorder that satisfies all of them while breaking the ordinary
        call still fails.
        """
        import logging

        from helpers import consent_lines

        store, dirs = tiers

        try:
            with caplog.at_level(logging.WARNING, logger="memtomem.privacy"):
                result = await store.add(self._CLEAN)
        finally:
            await store.close()

        assert result.get("error") is None, result
        assert Path(result["file"]).parent == dirs["user"]
        assert result["indexed_chunks"] >= 1
        assert consent_lines(caplog) == []

    # ── Review findings (Codex, 2026-09-06) ───────────────────────────────
    #
    # Each of the three below reproduces a case the first draft of this change
    # got wrong. They are grouped so a reader can see what the
    # ``_resolve_target_scope`` helper exists for.

    @staticmethod
    def _store(tmp_path, name, *, memory_dirs, project_memory_dirs):
        from memtomem.integrations.langgraph import MemtomemStore

        return MemtomemStore(
            config_overrides={
                "storage": {"sqlite_path": tmp_path / f"{name}.db"},
                "indexing": {
                    "memory_dirs": list(memory_dirs),
                    "project_memory_dirs": list(project_memory_dirs),
                },
                "embedding": {"dimension": 1024},
                "search": {"enable_dense": False},
            }
        )

    @pytest.mark.asyncio
    async def test_default_shaped_user_memory_dir_is_not_refused(
        self, tmp_path, monkeypatch, caplog
    ):
        """The most ordinary target there is must stay writable.

        The default user memory directory is ``~/.memtomem/memories``, which
        matches the ``project_shared`` path pattern exactly. A refusal keyed on
        "matches the pattern but is not registered" therefore rejects it — and
        the first draft of this change did, because its fixture used a plainly
        named user directory and so agreed with itself. A target covered by a
        configured ``memory_dirs`` entry is ``user``, whatever it is spelled
        like.
        """
        import logging

        from helpers import consent_lines, isolate_memtomem_env

        isolate_memtomem_env(monkeypatch)

        user_dir = tmp_path / ".memtomem" / "memories"
        user_dir.mkdir(parents=True)
        store = self._store(tmp_path, "u", memory_dirs=[user_dir], project_memory_dirs=[])
        target = user_dir / "note.md"

        try:
            with caplog.at_level(logging.WARNING, logger="memtomem.privacy"):
                result = await store.add(self._CLEAN, file=str(target))
        finally:
            await store.close()

        assert result.get("error") is None, result
        assert self._CLEAN in target.read_text(encoding="utf-8")
        assert consent_lines(caplog) == []

    @pytest.mark.asyncio
    async def test_a_target_the_indexer_would_file_differently_is_refused(
        self, tmp_path, monkeypatch, caplog
    ):
        """Ownership and spelling must agree, or nothing is written.

        ``<shared-root>/sub/.memtomem/memories.local/x.md`` lands inside the
        tree the shared root registered, so ownership says ``project_shared``.
        The indexer classifies by path shape, finds the inner pattern first,
        and would file the row as ``project_local``. Gating the write on the
        stricter answer protects the write and not the row: ``mem_edit`` and
        ``mem_delete`` read the tier from the row, so a note nobody could add
        without consent would afterwards be rewritable without it.

        Both halves matter, so both are asserted. Reverting the ownership rule
        to the raw classifier makes the two answers agree on ``project_local``
        and the write simply lands, unasked — which is why this test would
        rather see a refusal than a confirmation prompt.
        """
        import logging

        from helpers import consent_lines, isolate_memtomem_env

        isolate_memtomem_env(monkeypatch)

        user_dir = tmp_path / "user_mem"
        shared = tmp_path / "proj" / ".memtomem" / "memories"
        user_dir.mkdir(parents=True)
        shared.mkdir(parents=True)
        nested = shared / "sub" / ".memtomem" / "memories.local" / "x.md"

        store = self._store(tmp_path, "n", memory_dirs=[user_dir], project_memory_dirs=[shared])
        try:
            with caplog.at_level(logging.WARNING, logger="memtomem.privacy"):
                refused = await store.add(self._SECRET, file=str(nested), force_unsafe=True)
        finally:
            await store.close()

        assert refused["error"] == "tier_would_not_round_trip"
        assert "project_shared" in refused["detail"]
        assert "project_local" in refused["detail"]
        assert not nested.exists(), "nothing may land under the shared root"
        assert consent_lines(caplog) == []

    @pytest.mark.asyncio
    async def test_a_registered_root_that_cannot_name_its_own_tier_is_refused(
        self, tmp_path, monkeypatch, caplog
    ):
        """The same rule, caught from the other side.

        A ``project_memory_dirs`` entry whose own path is not canonical —
        ``<base>/registered`` with no ``.memtomem`` in it — cannot say which
        tier it is, so it does not get to decide one. That alone would let the
        target fall through to the user-directory branch and be gated as
        ``user`` while the indexer files the row as ``project_shared``: the
        original bug, reached through a different door. The round-trip check
        is what closes it, which is why this case belongs beside the one
        above rather than in its own corner.
        """
        import logging

        from helpers import consent_lines, isolate_memtomem_env

        isolate_memtomem_env(monkeypatch)

        base = tmp_path / "base"
        registered = base / "registered"
        registered.mkdir(parents=True)
        target = registered / "nested" / ".memtomem" / "memories" / "x.md"

        store = self._store(tmp_path, "m", memory_dirs=[base], project_memory_dirs=[registered])
        try:
            with caplog.at_level(logging.WARNING, logger="memtomem.privacy"):
                refused = await store.add(self._SECRET, file=str(target), force_unsafe=True)
        finally:
            await store.close()

        assert refused["error"] == "tier_would_not_round_trip"
        assert not target.exists(), "a force_unsafe secret must not reach a tracked path"
        assert consent_lines(caplog) == []

    @pytest.mark.asyncio
    async def test_a_project_root_beats_a_user_root_that_also_covers_the_target(
        self, tmp_path, monkeypatch, caplog
    ):
        """Registry precedence, pinned where the two registries overlap.

        Every other explicit-target case here keeps ``memory_dirs`` and
        ``project_memory_dirs`` on disjoint branches of the tree, so the order
        of the two lookups never shows. Here the user root is an *ancestor* of
        the registered project root, which is the ordinary shape once the user
        directory is ``~/.memtomem`` — and asking the user branch first would
        quietly demote a git-tracked target to ``user``.
        """
        import logging

        from helpers import consent_lines, isolate_memtomem_env

        isolate_memtomem_env(monkeypatch)

        root = tmp_path / "u"
        shared = root / ".memtomem" / "memories"
        shared.mkdir(parents=True)
        target = shared / "note.md"

        store = self._store(tmp_path, "p", memory_dirs=[root], project_memory_dirs=[shared])
        try:
            with caplog.at_level(logging.WARNING, logger="memtomem.privacy"):
                refused = await store.add(self._CLEAN, file=str(target))
                allowed = await store.add(
                    self._CLEAN, file=str(target), confirm_project_shared=True
                )
        finally:
            await store.close()

        assert refused["error"] == "project_shared_confirmation_required"
        assert allowed.get("error") is None, allowed
        assert len(consent_lines(caplog)) == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("reversed_order", [False, True])
    async def test_the_most_specific_covering_root_decides(
        self, tmp_path, monkeypatch, caplog, reversed_order
    ):
        """Two registered roots both cover the target; the inner one wins.

        Run in both configuration orders, because a rule that reads the first
        covering root rather than the most specific one is right half the time
        by luck. The inner tier is ``project_local``, which asks for nothing —
        so getting this wrong shows up as an unnecessary confirmation demand
        rather than a missing one, and would otherwise look harmless.
        """
        import logging

        from helpers import consent_lines, isolate_memtomem_env

        isolate_memtomem_env(monkeypatch)

        user_dir = tmp_path / "user_mem"
        outer = tmp_path / "proj" / ".memtomem" / "memories"
        inner = outer / "s" / ".memtomem" / "memories.local"
        user_dir.mkdir(parents=True)
        inner.mkdir(parents=True)
        roots = [inner, outer] if reversed_order else [outer, inner]
        target = inner / "note.md"

        store = self._store(
            tmp_path, f"s{int(reversed_order)}", memory_dirs=[user_dir], project_memory_dirs=roots
        )
        try:
            with caplog.at_level(logging.WARNING, logger="memtomem.privacy"):
                result = await store.add(self._CLEAN, file=str(target))
        finally:
            await store.close()

        # project_local: gitignored, so no confirmation and no consent record.
        assert result.get("error") is None, result
        assert self._CLEAN in target.read_text(encoding="utf-8")
        assert consent_lines(caplog) == []

    @pytest.mark.asyncio
    async def test_overlapping_registries_gate_the_derived_day_file_too(
        self, tmp_path, monkeypatch, caplog
    ):
        """An automatic destination in the tracked tier is still a tracked write.

        Configuration permits one directory in both ``memory_dirs`` and
        ``project_memory_dirs``. The derived day file then genuinely lives in
        the git-tracked tier, and both gates apply to it — the destination
        being chosen for the caller rather than by them changes who picked the
        path, not who reads the result.

        This was briefly deferred to #2322 on the theory that an automatic
        write should not grow a confirmation argument. #2322 turns out to
        enumerate four *other* writers and to say in its own text that the
        caller-controlled case belongs to this adapter, so there was nothing
        to defer it to.
        """
        import logging

        from helpers import consent_lines, isolate_memtomem_env

        isolate_memtomem_env(monkeypatch)

        overlap = tmp_path / "proj" / ".memtomem" / "memories"
        overlap.mkdir(parents=True)
        store = self._store(tmp_path, "o", memory_dirs=[overlap], project_memory_dirs=[overlap])

        try:
            with caplog.at_level(logging.WARNING, logger="memtomem.privacy"):
                refused = await store.add(self._CLEAN)
                allowed = await store.add(self._CLEAN, confirm_project_shared=True)
                blocked = await store.add(
                    self._SECRET, force_unsafe=True, confirm_project_shared=True
                )
        finally:
            await store.close()

        # Gate B: an unconfirmed automatic write into the tracked tier refuses.
        assert refused["error"] == "project_shared_confirmation_required"
        # Confirmed, it lands, and records exactly one consent.
        assert allowed.get("error") is None, allowed
        # Gate A: the tier is real, so the bypass valve stays shut.
        assert blocked["error"] == "redaction_blocked_project_shared"
        lines = consent_lines(caplog)
        assert len(lines) == 2, lines
        assert all("confirmed_via=langgraph_add" in line for line in lines)
