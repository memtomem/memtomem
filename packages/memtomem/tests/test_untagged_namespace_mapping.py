"""The resolver's ``None`` maps to the namespace it is stored as (#2738).

``IndexEngine.effective_namespace_for`` answers ``None`` for the untagged
carve-out, which the chunk model stores as ``UNTAGGED_NAMESPACE``. Surfaces
that substituted ``namespace.default_namespace`` for it stamped ``""`` when
that setting is empty, reported moves that never happened, and refused writes
to a file stored under ``""`` once the setting changed.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from helpers import StubCtx
from memtomem.memory_scope import namespace_mix_refusal
from memtomem.models import UNTAGGED_NAMESPACE, ChunkMetadata, stored_namespace
from memtomem.server.context import AppContext
from memtomem.server.tools import memory_crud
from memtomem.web.app import create_app
from memtomem.web.deps import require_configured


def _web_client(comp):
    app = create_app(lifespan=None, mode="dev")
    for name in ("storage", "index_engine", "search_pipeline", "config"):
        setattr(app.state, name, getattr(comp, name))
    app.state.project_root = None
    app.dependency_overrides[require_configured] = lambda: None
    return AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
    )


async def _stored(comp, source: Path) -> list[str]:
    chunks = await comp.storage.list_chunks_by_source(source, limit=None)
    return sorted({c.metadata.namespace for c in chunks})


async def _namespace_holding(comp, source: Path, needle: str) -> str:
    """The namespace of the one stored chunk whose content contains *needle*."""
    chunks = await comp.storage.list_chunks_by_source(source, limit=None)
    holders = [c for c in chunks if needle in c.content]
    assert len(holders) == 1, [c.content for c in chunks]
    return holders[0].metadata.namespace


def test_stored_namespace_maps_only_none():
    assert ChunkMetadata(source_file=Path()).namespace == UNTAGGED_NAMESPACE == "default"
    assert stored_namespace(None) == "default"
    # An explicit empty string is a namespace, not the carve-out.
    assert stored_namespace("") == ""
    assert stored_namespace("work") == "work"


@pytest.mark.parametrize(
    ("configured", "stored"), [("", "default"), ("default", "default"), ("custom", "custom")]
)
async def test_web_add_stamps_the_namespace_indexing_would(
    bm25_only_components, configured, stored
):
    comp, _ = bm25_only_components
    comp.config.namespace.default_namespace = configured
    async with _web_client(comp) as client:
        resp = await client.post("/api/add", json={"content": "web body", "file": "web.md"})
    assert resp.status_code == 200, resp.text
    assert await _stored(comp, Path(resp.json()["file"])) == [stored]


async def _seed_empty_namespace_file(comp, mem_dir: Path) -> Path:
    """A file whose rows are stored under ``""``, as web add wrote them before
    the fix when ``default_namespace`` was empty."""
    target = mem_dir / "legacy.md"
    target.write_text("## Legacy\n\nlegacy body\n", encoding="utf-8")
    assert not (await comp.index_engine.index_file(target, namespace="")).errors
    assert await _stored(comp, target) == [""]
    return target


@pytest.mark.parametrize("configured", ["default", "custom"])
async def test_web_add_keeps_a_file_stored_under_the_empty_namespace(
    bm25_only_components, configured
):
    """The resolver preserves ``""``; ``"" or default_namespace`` read it as
    unset, so the mix guard refused and the write targeted another namespace."""
    comp, mem_dir = bm25_only_components
    target = await _seed_empty_namespace_file(comp, mem_dir)
    comp.config.namespace.default_namespace = configured
    async with _web_client(comp) as client:
        resp = await client.post("/api/add", json={"content": "web body", "file": target.name})
    assert resp.status_code == 200, resp.text
    assert await _stored(comp, target) == [""]
    assert await _namespace_holding(comp, target, "web body") == ""


async def test_mcp_add_keeps_a_file_stored_under_the_empty_namespace(bm25_only_components):
    comp, mem_dir = bm25_only_components
    target = await _seed_empty_namespace_file(comp, mem_dir)
    ctx = StubCtx(AppContext.from_components(comp))

    result = await memory_crud.mem_add(content="mcp body", file=target.name, ctx=ctx)

    assert not result.startswith("Error"), result
    assert await _stored(comp, target) == [""]
    assert await _namespace_holding(comp, target, "mcp body") == ""


async def test_reassign_reports_no_move_for_an_untagged_file(bm25_only_components):
    comp, mem_dir = bm25_only_components
    comp.config.namespace.default_namespace = ""
    source = mem_dir / "plain.md"
    source.write_text("## Plain\n\nplain body\n", encoding="utf-8")
    assert not (await comp.index_engine.index_file(source)).errors
    assert await _stored(comp, source) == ["default"]

    stats = await comp.index_engine.index_file(source, reassign_namespaces=True)

    assert stats.namespace_moves == ()
    assert await _stored(comp, source) == ["default"]


async def test_mix_refusal_names_the_untagged_namespace(tmp_path):
    """A mixed file resolves to ``None``; the refusal must name what the write
    would store, and list only the namespaces it would move."""
    target = tmp_path / "mixed.md"
    target.write_text("some text", encoding="utf-8")
    err = await namespace_mix_refusal(
        index_engine=SimpleNamespace(effective_namespace_for=AsyncMock(return_value=None)),
        storage=SimpleNamespace(namespaces_for_source=AsyncMock(return_value=["default", "x"])),
        target=target,
        effective_ns=None,
        override_hint="-x",
    )
    assert err is not None
    assert "already holds namespace(s) 'x';" in err
    assert "resolves to namespace 'default'" in err
