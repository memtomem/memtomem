"""Derived namespaces pass the write gate or are skipped (#2946).

#2933 gated every caller-supplied ``namespace=``. The namespaces the indexer
derives itself — a rule template (placeholder or literal text),
``enable_auto_ns`` and ``namespace.default_namespace`` — could still produce a
name a reader cannot address by exact name (``a,b`` reads as a list, ``x*y``
as a glob, ``!system`` as the non-system selector). The resolver now skips
such a candidate and falls through to the next one, the way it already skips
a placeholder that expands to nothing.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest
from helpers import StubCtx

from memtomem.config import NamespaceConfig, NamespacePolicyRule
from memtomem.indexing.engine import _build_exclude_spec

pytestmark = pytest.mark.asyncio

REFUSED_DEFAULTS = ["x*y", "a,b", "!system", "  ", "agent-runtime:foo:bar"]


def _configure(comp, *, rules=(), enable_auto_ns=False, default_namespace="default"):
    """Install one namespace config on both the engine and the app config."""
    ns = NamespaceConfig(
        enable_auto_ns=enable_auto_ns,
        default_namespace=default_namespace,
        rules=list(rules),
    )
    comp.config.namespace = ns
    engine = comp.index_engine
    engine._ns_config = ns
    engine._ns_rule_specs = [(_build_exclude_spec([r.path_glob]), r) for r in ns.rules]


def _file(mem_dir: Path, folder: str, name: str = "n.md", body: str = "# Note\n\nzephyr") -> Path:
    path = mem_dir / folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


async def _stored(comp, source: Path) -> list[str]:
    chunks = await comp.storage.list_chunks_by_source(source, limit=None)
    return sorted({c.metadata.namespace for c in chunks})


@pytest.fixture
async def stack(bm25_only_components):
    return bm25_only_components


class TestResolver:
    async def test_placeholder_rule_falls_through_to_the_next_rule(self, stack):
        comp, mem_dir = stack
        _configure(
            comp,
            rules=[
                NamespacePolicyRule(path_glob="**/*.md", namespace="{parent}"),
                NamespacePolicyRule(path_glob="**/*.md", namespace="team"),
            ],
        )
        assert comp.index_engine._resolve_namespace(_file(mem_dir, "a,b"), None) == "team"

    async def test_literal_rule_text_is_gated_too(self, stack):
        comp, mem_dir = stack
        _configure(
            comp,
            rules=[NamespacePolicyRule(path_glob="**/*.md", namespace="a,b")],
            default_namespace="work",
        )
        assert comp.index_engine._resolve_namespace(_file(mem_dir, "x"), None) == "work"

    async def test_auto_ns_falls_through_to_the_default(self, stack):
        comp, mem_dir = stack
        _configure(comp, enable_auto_ns=True, default_namespace="work")
        engine = comp.index_engine
        assert engine._resolve_namespace(_file(mem_dir, "a,b"), None) == "work"
        if sys.platform != "win32":  # Windows refuses ``*`` in a folder name.
            assert engine._resolve_namespace(_file(mem_dir, "x*y"), None) == "work"
        # A bare ``agent-runtime`` folder is the strict agent-runtime shape.
        assert engine._resolve_namespace(_file(mem_dir, "agent-runtime"), None) == "work"

    @pytest.mark.parametrize("default", REFUSED_DEFAULTS)
    async def test_refused_default_resolves_untagged(self, stack, default):
        comp, mem_dir = stack
        _configure(comp, default_namespace=default)
        resolved, directed = comp.index_engine._resolve_namespace_directed(
            _file(mem_dir, "x"), None
        )
        assert (resolved, directed) == (None, False)

    async def test_valid_derivations_are_unchanged(self, stack):
        comp, mem_dir = stack
        engine = comp.index_engine
        # ``claude:-Users-…`` is what a Claude Code project rule produces; the
        # strict ``validate_namespace`` refuses its leading-dash segment, the
        # write gate must not.
        _configure(
            comp, rules=[NamespacePolicyRule(path_glob="**/*.md", namespace="claude:{parent}")]
        )
        assert engine._resolve_namespace(_file(mem_dir, "-Users-x"), None) == "claude:-Users-x"
        _configure(comp, enable_auto_ns=True)
        assert engine._resolve_namespace(_file(mem_dir, "proj"), None) == "proj"
        _configure(comp, default_namespace="work")
        assert engine._resolve_namespace(_file(mem_dir, "x"), None) == "work"

    async def test_a_skip_is_logged_once_per_name(self, stack, caplog):
        comp, mem_dir = stack
        _configure(comp, enable_auto_ns=True)
        engine = comp.index_engine
        files = [_file(mem_dir, "a,b", f"n{i}.md") for i in range(3)]
        with caplog.at_level(logging.WARNING, logger="memtomem.indexing.engine"):
            for fp in files:
                assert engine._resolve_namespace(fp, None) is None
        skips = [r for r in caplog.records if "enable_auto_ns skipped" in r.getMessage()]
        assert len(skips) == 1, [r.getMessage() for r in skips]
        assert "'a,b'" in skips[0].getMessage()


class TestIndexing:
    async def test_index_path_stores_the_fallthrough_namespace(self, stack):
        comp, mem_dir = stack
        _configure(comp, rules=[NamespacePolicyRule(path_glob="**/*.md", namespace="{parent}")])
        source = _file(mem_dir, "a,b")
        stats = await comp.index_engine.index_path(source)
        assert stats.indexed_chunks > 0
        assert await _stored(comp, source) == ["default"]
        names = {ns for ns, _ in await comp.storage.list_namespaces()}
        assert "a,b" not in names

    async def test_index_path_with_a_refused_default_stores_untagged(self, stack):
        comp, mem_dir = stack
        _configure(comp, default_namespace="x*y")
        source = _file(mem_dir, "x")
        await comp.index_engine.index_path(source)
        assert await _stored(comp, source) == ["default"]


class TestStoredLegacyName:
    """A source already stored under a refused name (written before the gate)."""

    async def _seed(self, comp, mem_dir: Path) -> Path:
        source = _file(mem_dir, "a,b")
        # The engine takes an explicit namespace as given; the gate lives on
        # the caller-facing surfaces, so this reproduces a pre-#2946 row.
        await comp.index_engine.index_path(source, namespace="a,b")
        assert await _stored(comp, source) == ["a,b"]
        _configure(comp, rules=[NamespacePolicyRule(path_glob="**/*.md", namespace="{parent}")])
        source.write_text("# Note\n\nzephyr, edited", encoding="utf-8")
        return source

    async def _assert_reindexed_in_place(self, comp, source: Path, stats) -> None:
        assert stats.errors == () and stats.indexed_chunks > 0, stats
        chunks = await comp.storage.list_chunks_by_source(source, limit=None)
        assert any("edited" in c.content for c in chunks)
        assert await _stored(comp, source) == ["a,b"]

    async def test_plain_reindex_preserves_it(self, stack):
        comp, mem_dir = stack
        source = await self._seed(comp, mem_dir)
        stats = await comp.index_engine.index_path(source)
        await self._assert_reindexed_in_place(comp, source, stats)

    async def test_force_preserves_it(self, stack):
        comp, mem_dir = stack
        source = await self._seed(comp, mem_dir)
        stats = await comp.index_engine.index_path(source, force=True)
        await self._assert_reindexed_in_place(comp, source, stats)

    async def test_reassign_moves_it_to_the_fallthrough_and_reports_it(self, stack):
        comp, mem_dir = stack
        source = await self._seed(comp, mem_dir)
        stats = await comp.index_engine.index_path(source, reassign_namespaces=True)
        assert await _stored(comp, source) == ["default"]
        assert stats.namespaces_reassigned == 1
        assert len(stats.namespace_moves) == 1
        assert "a,b" in repr(stats.namespace_moves[0])


class TestMemAddResponse:
    def _ctx(self, comp):
        from memtomem.server.context import AppContext

        return StubCtx(AppContext.from_components(comp))

    @pytest.mark.parametrize("default", ["x*y", "!system"])
    async def test_reports_where_the_rows_landed(self, stack, default):
        from memtomem.server.tools.memory_crud import mem_add

        comp, _ = stack
        _configure(comp, default_namespace=default)
        ctx = self._ctx(comp)
        out = await mem_add(content="zephyr fact", idempotency_key="k1", ctx=ctx)
        assert not out.startswith("Error"), out
        assert "- Namespace: default\n" in out
        assert {ns for ns, _ in await comp.storage.list_namespaces()} == {"default"}
        # The ledger replays the stored text, plus its replay footer.
        replay = await mem_add(content="zephyr fact", idempotency_key="k1", ctx=ctx)
        assert replay.startswith(out + "\n") and "[idempotent replay]" in replay

    async def test_batch_add_reports_where_the_rows_landed(self, stack):
        from memtomem.server.tools.memory_crud import mem_batch_add

        comp, _ = stack
        _configure(comp, default_namespace="x*y")
        out = await mem_batch_add(entries=[{"key": "k", "value": "v"}], ctx=self._ctx(comp))
        assert not out.startswith("Error"), out
        assert "- Namespace: default\n" in out
        assert {ns for ns, _ in await comp.storage.list_namespaces()} == {"default"}

    async def test_reports_the_rule_that_applied(self, stack):
        from memtomem.server.tools.memory_crud import mem_add

        comp, _ = stack
        _configure(
            comp,
            rules=[NamespacePolicyRule(path_glob="**/*.md", namespace="team")],
            default_namespace="x*y",
        )
        out = await mem_add(content="zephyr fact", ctx=self._ctx(comp))
        assert "- Namespace: team\n" in out
        assert {ns for ns, _ in await comp.storage.list_namespaces()} == {"team"}

    async def test_reports_a_preserved_legacy_namespace(self, stack):
        from memtomem.server.tools.memory_crud import mem_add

        comp, mem_dir = stack
        legacy = mem_dir / "legacy.md"
        legacy.write_text("# Old\n\nolder fact", encoding="utf-8")
        await comp.index_engine.index_path(legacy, namespace="a,b")
        _configure(comp, default_namespace="x*y")
        out = await mem_add(content="zephyr fact", file="legacy.md", ctx=self._ctx(comp))
        assert not out.startswith("Error"), out
        assert "- Namespace: a,b\n" in out
        assert await _stored(comp, legacy) == ["a,b"]

    @pytest.mark.parametrize("default", ["default", "", "work", "x*y"])
    async def test_a_write_that_indexes_nothing_reports_the_file_namespace(self, stack, default):
        from memtomem.server.tools.memory_crud import mem_batch_add

        comp, mem_dir = stack
        existing = mem_dir / "existing.md"
        existing.write_text("# Old\n\nolder fact", encoding="utf-8")
        await comp.index_engine.index_path(existing, namespace="team")
        _configure(comp, default_namespace=default)
        out = await mem_batch_add(
            entries=[{"key": "skipped", "value": ""}], file="existing.md", ctx=self._ctx(comp)
        )
        assert not out.startswith("Error"), out
        assert "- Namespace: team\n" in out
        assert await _stored(comp, existing) == ["team"]

    async def test_a_failed_lookup_is_reported_not_raised(self, stack, monkeypatch):
        from memtomem.server.tools.memory_crud import mem_batch_add

        comp, mem_dir = stack
        existing = mem_dir / "existing.md"
        existing.write_text("# Old\n\nolder fact", encoding="utf-8")
        await comp.index_engine.index_path(existing, namespace="team")

        # The store stops answering after the append is indexed: the mix guard
        # and the indexer itself still resolve, only the reply's lookup fails.
        engine = comp.index_engine
        real_index, real_resolve = engine.index_file, engine.effective_namespace_for
        down = False

        async def _index(*a, **k):
            nonlocal down
            stats = await real_index(*a, **k)
            down = True
            return stats

        async def _resolve(*a, **k):
            if down:
                raise RuntimeError("store down")
            return await real_resolve(*a, **k)

        monkeypatch.setattr(engine, "index_file", _index)
        monkeypatch.setattr(engine, "effective_namespace_for", _resolve)
        out = await mem_batch_add(
            entries=[{"key": "skipped", "value": ""}], file="existing.md", ctx=self._ctx(comp)
        )
        assert not out.startswith("Error"), out
        assert down
        assert "- Namespace: unknown (the chunk store did not answer)\n" in out

    async def test_a_valid_default_is_still_reported(self, stack):
        from memtomem.server.tools.memory_crud import mem_add

        comp, _ = stack
        _configure(comp, default_namespace="work")
        out = await mem_add(content="zephyr fact", ctx=self._ctx(comp))
        assert "- Namespace: work\n" in out
        assert {ns for ns, _ in await comp.storage.list_namespaces()} == {"work"}
