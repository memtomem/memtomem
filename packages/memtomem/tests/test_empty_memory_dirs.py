"""#1768 — empty ``indexing.memory_dirs`` is a valid "index nothing" state.

Read surfaces degrade gracefully (``mem_context_compose`` answers with no
pinned blocks); writes that need the user-tier base refuse with a
``ConfigError`` that names the config field, instead of dying with
``IndexError: list index out of range`` rendered as an opaque internal
error — or, worse, silently writing under the server's cwd.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from helpers import StubCtx, isolate_memtomem_env
from memtomem.server.context import AppContext
from memtomem.server.tools import memory_crud
from memtomem.server.tools import pinned as pinned_tools


# ---------------------------------------------------------------------------
# MCP surface
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mem_context_compose_returns_bundle_not_internal_error(bm25_only_components):
    """The issue's headline repro: compose must not crash on empty dirs."""
    comp, _mem_dir = bm25_only_components
    comp.config.indexing.memory_dirs = []
    ctx = StubCtx(AppContext.from_components(comp))
    out = await pinned_tools.mem_context_compose(query=None, ctx=ctx)
    assert "internal error" not in out
    assert json.loads(out)["pinned"] == []


@pytest.mark.asyncio
async def test_mem_pinned_set_names_the_config_field(bm25_only_components):
    comp, _mem_dir = bm25_only_components
    comp.config.indexing.memory_dirs = []
    ctx = StubCtx(AppContext.from_components(comp))
    out = await pinned_tools.mem_pinned_set("block", "harmless content", ctx=ctx)
    assert out.startswith("Error:")
    assert "indexing.memory_dirs is empty" in out
    assert "internal error" not in out


@pytest.mark.asyncio
async def test_mem_add_default_target_errors_instead_of_writing_cwd(
    bm25_only_components, monkeypatch, tmp_path
):
    comp, _mem_dir = bm25_only_components
    comp.config.indexing.memory_dirs = []
    ctx = StubCtx(AppContext.from_components(comp))
    cwd = tmp_path / "server-cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    out = await memory_crud.mem_add(content="a harmless note", ctx=ctx)
    assert "indexing.memory_dirs is empty" in out
    assert list(cwd.rglob("*.md")) == []


@pytest.mark.asyncio
async def test_mem_batch_add_default_target_errors_instead_of_writing_cwd(
    bm25_only_components, monkeypatch, tmp_path
):
    comp, _mem_dir = bm25_only_components
    comp.config.indexing.memory_dirs = []
    ctx = StubCtx(AppContext.from_components(comp))
    cwd = tmp_path / "server-cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    out = await memory_crud.mem_batch_add(
        entries=[{"key": "k", "value": "a harmless note"}], ctx=ctx
    )
    assert "indexing.memory_dirs is empty" in out
    assert list(cwd.rglob("*.md")) == []


# ---------------------------------------------------------------------------
# CLI ``mm mem add`` — user-tier default target
# ---------------------------------------------------------------------------


def test_mm_mem_add_user_scope_refuses_instead_of_legacy_fallback(monkeypatch, tmp_path):
    """Pre-fix the CLI fell back to the historical ``~/.memtomem/memories``
    literal, writing into a directory the active config disabled."""
    from click.testing import CliRunner

    from memtomem.cli.memory import add

    comp = AsyncMock()
    comp.config.indexing.memory_dirs = []
    comp.config.indexing.project_memory_dirs = []

    @asynccontextmanager
    async def fake_components():
        yield comp

    monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", fake_components)
    result = CliRunner().invoke(add, ["a harmless note"])
    assert result.exit_code != 0
    assert "indexing.memory_dirs is empty" in result.output
    assert "Traceback" not in result.output


# ---------------------------------------------------------------------------
# CLI ``mm context memory-migrate`` — user-tier resolution
# ---------------------------------------------------------------------------


def test_memory_migrate_touching_user_tier_refuses_on_empty_memory_dirs(monkeypatch, tmp_path):
    """``--apply`` moves files; an empty ``memory_dirs`` must refuse before
    resolving the user tier to the historical default directory."""
    from click.testing import CliRunner

    from memtomem.cli.context_cmd import memory_migrate_cmd

    project_root = tmp_path / "proj"
    proj_shared = project_root / ".memtomem" / "memories"
    proj_shared.mkdir(parents=True)
    (project_root / ".git").mkdir()
    src = tmp_path / "rule.md"
    src.write_text("## Rule\n\nharmless body.\n", encoding="utf-8")

    comp = AsyncMock()
    comp.config.indexing.memory_dirs = []
    comp.config.indexing.project_memory_dirs = [proj_shared]

    @asynccontextmanager
    async def fake_components():
        yield comp

    monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", fake_components)
    monkeypatch.chdir(project_root)
    result = CliRunner().invoke(
        memory_migrate_cmd, [str(src), "--from", "user", "--to", "project_shared"]
    )
    assert result.exit_code != 0
    assert "indexing.memory_dirs is empty" in result.output
    assert src.exists()  # nothing moved


# ---------------------------------------------------------------------------
# LangGraph store (root=None resolves the user tier)
# ---------------------------------------------------------------------------


def _patch_empty_dirs_config(monkeypatch):
    import memtomem.config as _cfg

    isolate_memtomem_env(monkeypatch)
    monkeypatch.setattr(_cfg, "load_config_d", lambda *args, **kwargs: None)

    def _empty_dirs(config):
        config.indexing.memory_dirs = []

    monkeypatch.setattr(_cfg, "load_config_overrides", _empty_dirs)


def test_langgraph_store_default_root_requires_user_memory_dir(monkeypatch):
    pytest.importorskip("langgraph")
    from memtomem.config import EmbeddingConfig
    from memtomem.errors import ConfigError
    from memtomem.integrations.langgraph_store import MemtomemBaseStore

    _patch_empty_dirs_config(monkeypatch)
    with pytest.raises(ConfigError, match="indexing.memory_dirs"):
        MemtomemBaseStore(embedding=EmbeddingConfig(provider="none", dimension=0))


def test_langgraph_store_project_tier_ok_with_empty_memory_dirs(monkeypatch, tmp_path):
    pytest.importorskip("langgraph")
    from memtomem.config import EmbeddingConfig
    from memtomem.integrations.langgraph_store import MemtomemBaseStore

    _patch_empty_dirs_config(monkeypatch)
    store = MemtomemBaseStore(
        scope="project_local",
        project_root=tmp_path / "proj",
        embedding=EmbeddingConfig(provider="none", dimension=0),
    )
    try:
        expected = tmp_path / "proj" / ".memtomem" / "memories.local" / "langgraph-store"
        assert store.root == expected.resolve()
    finally:
        store.close()


# ---------------------------------------------------------------------------
# CLI review approve (writes a dated file under the user tier)
# ---------------------------------------------------------------------------


def test_review_approve_empty_memory_dirs_is_clean_cli_error(monkeypatch):
    from click.testing import CliRunner

    from memtomem.cli.review_cmd import review
    from memtomem.config import Mem2MemConfig

    config = Mem2MemConfig()
    config.indexing.memory_dirs = []
    storage = SimpleNamespace(
        get_memory_candidate=AsyncMock(
            return_value={
                "id": "candidate-1",
                "status": "pending",
                "destination": "daily",
                "kind": "fact",
                "content": "a harmless durable fact",
            }
        ),
        claim_memory_candidate=AsyncMock(return_value={"id": "candidate-1"}),
        release_memory_candidate=AsyncMock(),
    )

    @asynccontextmanager
    async def fake_components():
        yield SimpleNamespace(storage=storage, config=config)

    monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", fake_components)
    result = CliRunner().invoke(review, ["approve", "candidate-1"])
    assert result.exit_code != 0
    assert "indexing.memory_dirs is empty" in result.output
    assert "Traceback" not in result.output
    # The claim was rolled back, not left dangling.
    storage.release_memory_candidate.assert_awaited_once()
