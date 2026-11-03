"""The store's components see none of the developer's memtomem configuration.

ADR-0042 B1: ``config.json``, ``config.d`` and ``MEMTOMEM_*`` never reach the
store's components. B7: the project registry is read from them on purpose.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from langchain_memtomem import MemtomemPipelineStore


async def test_config_d_and_environment_do_not_move_the_index(
    root: Path, isolated_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    elsewhere = tmp_path / "elsewhere.db"
    config_d = isolated_home / ".memtomem" / "config.d"
    config_d.mkdir(parents=True)
    (config_d / "10-redirect.json").write_text(
        json.dumps({"storage": {"sqlite_path": str(elsewhere)}, "search": {"cache_ttl": 1.0}})
    )
    (isolated_home / ".memtomem" / "config.json").write_text(
        json.dumps({"storage": {"sqlite_path": str(tmp_path / "overrides.db")}})
    )
    monkeypatch.setenv("MEMTOMEM_STORAGE__SQLITE_PATH", str(tmp_path / "env.db"))
    store = await MemtomemPipelineStore.open(root)
    try:
        await store.aput(("users",), "k", {"text": "apples"})
        assert [i.key for i in await store.asearch(("users",), query="apples")] == ["k"]
        assert store._comp is not None
        assert store._comp.config.storage.sqlite_path == root / "index.db"
        assert store._comp.config.search.cache_ttl == 30.0
    finally:
        await store.aclose()
    assert (root / "index.db").exists()
    assert not elsewhere.exists()
    assert not (tmp_path / "overrides.db").exists()
    assert not (tmp_path / "env.db").exists()


async def test_nothing_is_written_under_the_developers_memtomem(
    root: Path, isolated_home: Path
) -> None:
    store = await MemtomemPipelineStore.open(root)
    try:
        await store.aput(("users",), "k", {"text": "apples"})
        await store.asearch(("users",), query="apples")
        await store.adelete(("users",), "k")
    finally:
        await store.aclose()
    personal = isolated_home / ".memtomem"
    written = sorted(str(p.relative_to(personal)) for p in personal.rglob("*") if p.is_file())
    assert [w for w in written if not w.startswith("cache/")] == []


async def test_callers_config_sections_apply_and_owned_ones_are_refused(root: Path) -> None:
    store = await MemtomemPipelineStore.open(root, config={"search": {"cache_ttl": 2.5}})
    try:
        assert store._comp is not None
        assert store._comp.config.search.cache_ttl == 2.5
        assert store._comp.config.search.system_namespace_prefixes == []
    finally:
        await store.aclose()
    with pytest.raises(ValueError, match="storage"):
        await MemtomemPipelineStore.open(root, config={"storage": {"sqlite_path": "x"}})
    with pytest.raises(ValueError, match="indexing"):
        await MemtomemPipelineStore.open(root, config={"indexing": {"memory_dirs": []}})
    with pytest.raises(ValueError):
        await MemtomemPipelineStore.open(root, config={"search": {"default_top_k": 0}})


async def test_rerank_section_is_reachable(root: Path) -> None:
    store = await MemtomemPipelineStore.open(root, config={"rerank": {"enabled": False}})
    try:
        assert store._comp is not None
        assert store._comp.config.rerank.enabled is False
    finally:
        await store.aclose()


def _register_project(home: Path, memories_dir: Path) -> None:
    (home / ".memtomem").mkdir(parents=True, exist_ok=True)
    (home / ".memtomem" / "config.json").write_text(
        json.dumps({"indexing": {"project_memory_dirs": [str(memories_dir)]}})
    )


async def test_a_root_in_a_registered_project_local_tier_searches_its_own_rows(
    isolated_home: Path, tmp_path: Path
) -> None:
    tier = tmp_path / "proj" / ".memtomem" / "memories.local"
    tier.mkdir(parents=True)
    _register_project(isolated_home, tier)
    store = await MemtomemPipelineStore.open(tier / "app-store")
    try:
        assert store.scope == "project_local"
        await store.aput(("users",), "k", {"text": "apples"})
        assert [i.key for i in await store.asearch(("users",), query="apples")] == ["k"]
    finally:
        await store.aclose()


async def test_a_root_in_a_registered_project_shared_tier_needs_consent(
    isolated_home: Path, tmp_path: Path
) -> None:
    tier = tmp_path / "proj" / ".memtomem" / "memories"
    tier.mkdir(parents=True)
    _register_project(isolated_home, tier)
    with pytest.raises(ValueError, match="confirm_project_shared"):
        await MemtomemPipelineStore.open(tier / "app-store")
    store = await MemtomemPipelineStore.open(tier / "app-store", confirm_project_shared=True)
    try:
        assert store.scope == "project_shared"
        await store.aput(("users",), "k", {"text": "apples"})
        assert [i.key for i in await store.asearch(("users",), query="apples")] == ["k"]
    finally:
        await store.aclose()


async def test_a_secret_is_refused_before_anything_is_written(
    root: Path, store: MemtomemPipelineStore
) -> None:
    secret = {"text": "token ghp_" + "A" * 36 + " leaks"}
    with pytest.raises(ValueError, match="privacy"):
        await store.aput(("users",), "k", secret)
    assert await store.aget(("users",), "k") is None
    assert store._paths(("users",), "k").projection.exists() is False
    assert await store.asearch(("users",), query="token", limit=10) == []


async def test_operations_before_open_are_refused(root: Path) -> None:
    store = MemtomemPipelineStore(root)
    with pytest.raises(RuntimeError, match="not open"):
        await store.aput(("users",), "k", {"text": "apples"})
