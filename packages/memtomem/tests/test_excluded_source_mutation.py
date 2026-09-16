"""#2488 (narrow): a source indexing now skips is refused before any CRUD write.

Writing it and then re-indexing would return zeroed stats without raising, so
the old chunks would stay searchable beside the new bytes. Each surface is
driven with a writable, visible chunk carrying valid provenance, so the
read-only, consent and provenance gates that run first cannot be what refuses.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from httpx import ASGITransport, AsyncClient

from helpers import StubCtx
from memtomem.indexing.engine import resolve_owning_memory_dir
from memtomem.server.context import AppContext
from memtomem.server.tools import memory_crud
from memtomem.source_provenance import EXCLUDED_SOURCE_DETAIL
from memtomem.tools import memory_mutation, memory_writer
from memtomem.web.app import create_app
from memtomem.web.deps import require_configured
from memtomem.web.routes import chunks as chunk_routes

ORIGINAL = "## Alpha\n\nAlpha body\n"
SURFACES = ("mcp_edit", "mcp_delete", "web_edit", "web_delete")


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, timeout=60)


def _repository_with_worktree(root: Path) -> Path:
    if shutil.which("git") is None:
        pytest.skip("needs a git binary")
    root.mkdir(exist_ok=True)
    _git("init", "-q", ".", cwd=root)
    _git("config", "user.email", "t@example.invalid", cwd=root)
    _git("config", "user.name", "t", cwd=root)
    (root / "keep.md").write_text("# keep\n", encoding="utf-8")
    _git("add", "keep.md", cwd=root)
    _git("commit", "-qm", "init", cwd=root)
    wt = root / ".worktrees/wt"
    _git("worktree", "add", "-q", str(wt), "-b", "wt", cwd=root)
    return wt


@pytest.fixture(params=["pattern", "owned_worktree", "unowned_worktree"])
async def excluded_source(request, bm25_only_components, tmp_path):
    """An indexed source that the current configuration then excludes.

    ``pattern``: an ``indexing.exclude_patterns`` entry added after indexing.
    ``owned_worktree``: a worktree registered as its own root while indexed, then
    unregistered, so the parent root owns it and finds a nested worktree.
    ``unowned_worktree``: the same inside a repository outside every configured
    root, so no root owns it and its enclosing repository is the bound (#2486).
    """
    comp, mem_dir = bm25_only_components
    indexing = comp.config.indexing
    if request.param == "pattern":
        source = mem_dir / "note.md"
        source.write_text(ORIGINAL, encoding="utf-8")
        await _index(comp, source)
        indexing.exclude_patterns = [*indexing.exclude_patterns, "**/note.md"]
    else:
        parent = mem_dir if request.param == "owned_worktree" else tmp_path / "outside"
        wt = _repository_with_worktree(parent)
        source = wt / "note.md"
        source.write_text(ORIGINAL, encoding="utf-8")
        indexing.memory_dirs = [mem_dir, wt]
        await _index(comp, source)
        indexing.memory_dirs = [mem_dir]
        owner = resolve_owning_memory_dir(source, indexing.all_index_roots())
        assert (owner is None) is (request.param == "unowned_worktree")
    assert comp.index_engine.is_excluded(source)
    (chunk,) = await comp.storage.list_chunks_by_source(source)
    assert chunk.metadata.source_span_hash
    assert not chunk.metadata.source_read_only
    return comp, source, chunk


async def _index(comp, source: Path) -> None:
    stats = await comp.index_engine.index_file(source, already_scanned=True)
    assert not stats.errors
    assert stats.indexed_chunks == 1


async def _request(comp, surface, chunk_id):
    if surface.startswith("mcp"):
        ctx = StubCtx(AppContext.from_components(comp))
        if surface.endswith("edit"):
            return await memory_crud.mem_edit(str(chunk_id), "NEW BODY", ctx=ctx)
        return await memory_crud.mem_delete(chunk_id=str(chunk_id), ctx=ctx)
    app = create_app(lifespan=None, mode="dev")
    for name in ("storage", "index_engine", "search_pipeline", "config"):
        setattr(app.state, name, getattr(comp, name))
    app.dependency_overrides[require_configured] = lambda: None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        if surface.endswith("edit"):
            return await client.patch(f"/api/chunks/{chunk_id}", json={"new_content": "NEW BODY"})
        return await client.delete(f"/api/chunks/{chunk_id}")


def _assert_excluded(result):
    if isinstance(result, str):
        assert result == f"Error: {EXCLUDED_SOURCE_DETAIL}"
    else:
        assert result.status_code == 409, result.text
        assert result.json()["detail"] == EXCLUDED_SOURCE_DETAIL


@pytest.mark.parametrize("surface", SURFACES)
async def test_an_excluded_source_is_refused_before_any_write(
    excluded_source, monkeypatch, surface
):
    comp, source, chunk = excluded_source
    source_before = source.read_bytes()
    rows_before = comp.storage._get_db().execute("SELECT * FROM chunks").fetchall()
    index = AsyncMock(wraps=comp.index_engine.index_file)
    monkeypatch.setattr(comp.index_engine, "index_file", index)
    # MCP imports the writers inside the tool body (from ``memory_writer``); the
    # web routes bind them at import. Spy on both homes.
    writes = []
    for module, name in (
        (memory_writer, "replace_chunk_body"),
        (memory_writer, "remove_lines"),
        (chunk_routes, "replace_chunk_body"),
        (chunk_routes, "remove_lines"),
    ):
        spy = Mock(wraps=getattr(module, name))
        monkeypatch.setattr(module, name, spy)
        writes.append(spy)
    restores = []
    for module in (memory_crud, memory_mutation):
        restore = Mock(wraps=module.restore_pre_image_quietly)
        monkeypatch.setattr(module, "restore_pre_image_quietly", restore)
        restores.append(restore)

    _assert_excluded(await _request(comp, surface, chunk.id))

    assert source.read_bytes() == source_before
    assert comp.storage._get_db().execute("SELECT * FROM chunks").fetchall() == rows_before
    index.assert_not_awaited()
    for spy in writes:
        spy.assert_not_called()
    for restore in restores:
        restore.assert_not_called()


@pytest.mark.parametrize("surface", SURFACES)
async def test_a_source_that_is_not_excluded_still_mutates(bm25_only_components, surface):
    """The control: the guard refuses exclusion, not every edit."""
    comp, mem_dir = bm25_only_components
    source = mem_dir / "note.md"
    source.write_text(ORIGINAL, encoding="utf-8")
    await _index(comp, source)
    (chunk,) = await comp.storage.list_chunks_by_source(source)

    result = await _request(comp, surface, chunk.id)

    if isinstance(result, str):
        assert not result.startswith("Error"), result
    else:
        assert result.status_code == 200, result.text
    assert source.read_text(encoding="utf-8") != ORIGINAL
