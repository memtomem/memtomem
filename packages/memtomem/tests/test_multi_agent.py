"""Tests for multi-agent namespace helpers (``mem_agent_*``).

The sanitizer used by ``mem_agent_register`` / ``mem_agent_search`` lives in
``storage/sqlite_namespace.py`` as ``sanitize_namespace_segment`` — shared
with the ingest pipeline. These tests pin the behavior the multi-agent tool
relies on (single-segment `agent_id` sanitization so the generated
``agent-runtime:{id}`` namespace stays at depth 1).

``TestDefaultIsolation`` pins the multi-agent default search isolation
behaviour: ``agent-runtime:`` lives in ``system_namespace_prefixes`` by
default so one agent's private chunks do not leak into another agent's
``mem_search`` results. Removing the prefix would silently break the
isolation contract advertised in the multi-agent guide, so the assertion
imports the constant directly (per ``feedback_pin_test_constant_over_source_scan``).
"""

from __future__ import annotations

import pytest

from memtomem.config import Mem2MemConfig
from memtomem.constants import (
    AGENT_NAMESPACE_PREFIX,
    SHARED_NAMESPACE,
    _DEFAULT_SYSTEM_PREFIXES,
    default_system_prefixes,
)
from memtomem.server.component_factory import close_components, create_components
from memtomem.storage.sqlite_namespace import sanitize_namespace_segment

from helpers import make_chunk


class TestSanitizeNamespaceSegment:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("alpha", "alpha"),
            ("  spaced  ", "spaced"),
            ("foo/bar", "foo_bar"),
            ("a/b/c", "a_b_c"),
            ("name!with?specials", "name_with_specials"),
            ("ok.chars-allowed:1@host", "ok.chars-allowed:1@host"),
            ("with space", "with space"),
            ("한글도허용", "한글도허용"),
        ],
    )
    def test_sanitize_replaces_disallowed(self, raw, expected):
        assert sanitize_namespace_segment(raw) == expected

    def test_sanitize_preserves_allowed_chars(self):
        allowed = "abc_123-xyz.foo:bar@host"
        assert sanitize_namespace_segment(allowed) == allowed

    def test_sanitize_pure_slash_collapses_to_underscore(self):
        """``agent_id="/"`` must not produce ``agent-runtime://`` (double separator)."""
        assert sanitize_namespace_segment("/") == "_"


class TestDefaultIsolation:
    """Default ``system_namespace_prefixes`` hides ``agent-runtime:`` chunks
    from un-pinned ``mem_search``.

    Removing ``agent-runtime:`` from the default would silently leak one
    agent's private memories into another agent's day-to-day search
    results — exactly the isolation guarantee the multi-agent guide
    advertises. The pin imports the constant directly so a future
    refactor cannot drift the literal in only one place.
    """

    def test_constant_includes_agent_runtime_prefix(self):
        """``_DEFAULT_SYSTEM_PREFIXES`` must include both archive and agent-runtime."""
        assert "archive:" in _DEFAULT_SYSTEM_PREFIXES
        assert AGENT_NAMESPACE_PREFIX in _DEFAULT_SYSTEM_PREFIXES

    def test_factory_returns_fresh_list(self):
        """``default_system_prefixes`` must hand back a fresh list each call.

        Pydantic field defaults that share a list instance leak mutations
        across model instantiations — a classic gotcha that this test
        catches at the seam.
        """
        first = default_system_prefixes()
        second = default_system_prefixes()
        assert first == second
        assert first is not second
        first.append("mutate:")
        assert "mutate:" not in second

    def test_search_config_default_includes_agent_runtime(self):
        """A freshly built ``Mem2MemConfig`` carries the default in place."""
        cfg = Mem2MemConfig()
        prefixes = list(cfg.search.system_namespace_prefixes)
        assert "archive:" in prefixes
        assert AGENT_NAMESPACE_PREFIX in prefixes


class TestAgentRuntimeIsolationPipeline:
    """End-to-end pipeline pin: chunk in ``agent-runtime:planner`` is hidden
    from ``namespace=None`` search (matches the ``archive:*`` flow in
    ``test_trust_ux``) but reachable when the caller pins the namespace.
    """

    @pytest.fixture
    async def isolated_components(self, tmp_path, monkeypatch):
        db_path = tmp_path / "isolation.db"
        mem_dir = tmp_path / "memories"
        mem_dir.mkdir()

        for var in (
            "MEMTOMEM_EMBEDDING__PROVIDER",
            "MEMTOMEM_EMBEDDING__MODEL",
            "MEMTOMEM_EMBEDDING__DIMENSION",
            "MEMTOMEM_STORAGE__SQLITE_PATH",
            "MEMTOMEM_INDEXING__MEMORY_DIRS",
        ):
            monkeypatch.delenv(var, raising=False)

        config = Mem2MemConfig()
        config.storage.sqlite_path = db_path
        config.indexing.memory_dirs = [mem_dir]
        config.embedding.dimension = 1024
        config.search.enable_dense = False  # BM25-only — no embedder needed

        import memtomem.config as _cfg

        monkeypatch.setattr(_cfg, "load_config_overrides", lambda c: None)

        comp = await create_components(config)
        try:
            yield comp
        finally:
            await close_components(comp)

    async def test_default_search_excludes_agent_runtime_chunks(self, isolated_components):
        """``mem_search`` (namespace=None) must not return another agent's private chunks."""
        comp = isolated_components

        public = make_chunk("planner discusses architecture", namespace="default")
        private = make_chunk(
            "planner private architecture notes",
            namespace=f"{AGENT_NAMESPACE_PREFIX}planner",
        )
        await comp.storage.upsert_chunks([public, private])

        results, stats = await comp.search_pipeline.search("architecture", top_k=10)
        result_namespaces = {r.chunk.metadata.namespace for r in results}

        assert "default" in result_namespaces
        assert f"{AGENT_NAMESPACE_PREFIX}planner" not in result_namespaces
        assert stats.hidden_system_ns >= 1

    async def test_explicit_agent_namespace_returns_chunks(self, isolated_components):
        """``mem_agent_search`` (which pins ``namespace=``) reaches the same chunks."""
        comp = isolated_components

        private = make_chunk(
            "planner private notes",
            namespace=f"{AGENT_NAMESPACE_PREFIX}planner",
        )
        await comp.storage.upsert_chunks([private])

        results, _ = await comp.search_pipeline.search(
            "planner",
            top_k=10,
            namespace=f"{AGENT_NAMESPACE_PREFIX}planner",
        )
        assert any(
            r.chunk.metadata.namespace == f"{AGENT_NAMESPACE_PREFIX}planner" for r in results
        )

    async def test_shared_namespace_visible_in_default_search(self, isolated_components):
        """The ``shared`` namespace is *not* a system prefix — cross-agent
        knowledge stays surfaceable via plain ``mem_search``.
        """
        comp = isolated_components

        shared = make_chunk("shared cross-agent knowledge", namespace=SHARED_NAMESPACE)
        await comp.storage.upsert_chunks([shared])

        results, _ = await comp.search_pipeline.search("knowledge", top_k=10)
        assert any(r.chunk.metadata.namespace == SHARED_NAMESPACE for r in results)
