"""ADR-0011 PR-D — write surface gate tests.

Three load-bearing pins for the memory write surface:

1. **Gate B explicit-flag-and-confirm.** ``mem_add(scope='project_shared',
   ...)`` without ``confirm_project_shared=True`` rejects with a clear
   error. Mirrored on ``mem_batch_add``.
2. **Gate A unbypassable on project_shared.** ``force_unsafe=True``
   plus ``scope='project_shared'`` plus a hit returns
   ``blocked_project_shared`` regardless of the surface (single
   ``mem_add``, batch ``mem_batch_add``).
3. **Inferred scope on edit.** ``mem_edit`` reads the loaded chunk's
   ``metadata.scope`` and feeds it to the guard — a client cannot
   bypass Gate A by omitting an explicit scope param.

The mocks in this file pre-stage the canonical pieces ``_mem_add_core``
calls: the embedding mismatch check, the AppContext, the index_engine
file-index, and the storage chunk lookup.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from helpers import StubCtx
from memtomem import privacy
from memtomem.models import Chunk, ChunkMetadata
from memtomem.server.context import AppContext
from memtomem.server.tools import memory_crud

_SECRET = "api_key=AKIA1234567890ABCDEF"


@pytest.fixture(autouse=True)
def _reset_counters():
    privacy.reset_for_tests()
    yield
    privacy.reset_for_tests()


# ---------------------------------------------------------------------------
# Gate B: explicit-flag-and-confirm
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mem_add_project_shared_without_confirm_rejects(bm25_only_components):
    comp, _mem_dir = bm25_only_components
    app = AppContext.from_components(comp)
    ctx = StubCtx(app)
    out = await memory_crud.mem_add(
        content="harmless team rule",
        scope="project_shared",
        ctx=ctx,
    )
    assert "confirm_project_shared=True" in out
    assert "Error" in out


@pytest.mark.asyncio
async def test_mem_batch_add_project_shared_without_confirm_rejects(bm25_only_components):
    comp, _mem_dir = bm25_only_components
    app = AppContext.from_components(comp)
    ctx = StubCtx(app)
    out = await memory_crud.mem_batch_add(
        entries=[{"key": "k", "value": "harmless team rule"}],
        scope="project_shared",
        ctx=ctx,
    )
    assert "confirm_project_shared=True" in out
    assert "Error" in out


# ---------------------------------------------------------------------------
# Gate A: project_shared force_unsafe is hard-refused on every surface
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mem_add_project_shared_force_unsafe_blocked(bm25_only_components):
    comp, _mem_dir = bm25_only_components
    app = AppContext.from_components(comp)
    ctx = StubCtx(app)
    out = await memory_crud.mem_add(
        content=_SECRET,
        scope="project_shared",
        confirm_project_shared=True,
        force_unsafe=True,
        ctx=ctx,
    )
    assert "force_unsafe=True is not permitted" in out
    assert "git history is forever" in out


@pytest.mark.asyncio
async def test_mem_batch_add_project_shared_force_unsafe_blocked(bm25_only_components):
    comp, _mem_dir = bm25_only_components
    app = AppContext.from_components(comp)
    ctx = StubCtx(app)
    out = await memory_crud.mem_batch_add(
        entries=[
            {"key": "clean", "value": "harmless"},
            {"key": "secret", "value": _SECRET},
        ],
        scope="project_shared",
        confirm_project_shared=True,
        force_unsafe=True,
        ctx=ctx,
    )
    assert "force_unsafe=True is not permitted" in out
    assert "Whole batch rejected" in out
    # The blocked_project_shared counter records once per hit item.
    snap = privacy.snapshot()
    assert snap["by_tool"]["mem_batch_add"]["blocked_project_shared"] == 1
    # Critically: clean entries do NOT register a pass on a rejected
    # batch (transactional reject preserved).
    assert snap["by_tool"]["mem_batch_add"]["pass"] == 0


# ---------------------------------------------------------------------------
# Default scope behavior preserved (user)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mem_add_default_user_scope_force_unsafe_still_works(bm25_only_components):
    """Existing user-scope force_unsafe path is unchanged by ADR-0011."""
    comp, _mem_dir = bm25_only_components
    app = AppContext.from_components(comp)
    ctx = StubCtx(app)
    out = await memory_crud.mem_add(
        content=_SECRET,
        force_unsafe=True,  # user scope by default
        ctx=ctx,
    )
    # No project_shared error; the old bypass path proceeds.
    assert "force_unsafe=True is not permitted" not in out
    assert "Memory added to" in out
    snap = privacy.snapshot()
    assert snap["by_tool"]["mem_add"]["bypassed"] == 1


# ---------------------------------------------------------------------------
# mem_edit inferred scope — gate sees chunk.metadata.scope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mem_edit_inferred_scope_blocks_project_shared_force_unsafe(
    bm25_only_components, monkeypatch
):
    comp, _mem_dir = bm25_only_components
    app = AppContext.from_components(comp)
    ctx = StubCtx(app)

    proj = Path("/tmp/proj_x")
    chunk_id = uuid4()
    fake_chunk = Chunk(
        content="original",
        metadata=ChunkMetadata(
            source_file=proj / ".memtomem" / "memories" / "x.md",
            scope="project_shared",
            project_root=proj,
        ),
        embedding=[0.1] * 1024,
    )
    monkeypatch.setattr(comp.storage, "get_chunk", AsyncMock(return_value=fake_chunk))

    out = await memory_crud.mem_edit(
        chunk_id=str(chunk_id),
        new_content=_SECRET,
        force_unsafe=True,
        ctx=ctx,
    )
    # The edit surface inferred scope=project_shared from the loaded
    # chunk's metadata; force_unsafe=True is hard-refused.
    assert "force_unsafe=True is not permitted" in out
    assert "git history is forever" in out
    snap = privacy.snapshot()
    assert snap["by_tool"]["mem_edit"]["blocked_project_shared"] == 1


@pytest.mark.asyncio
async def test_mem_edit_inferred_user_scope_force_unsafe_proceeds(
    bm25_only_components, monkeypatch, tmp_path
):
    """A user-scope chunk's edit surface still allows force_unsafe (no regression)."""
    comp, _mem_dir = bm25_only_components
    app = AppContext.from_components(comp)
    ctx = StubCtx(app)
    src = tmp_path / "u.md"
    src.write_text("## hi\n\noriginal\n")
    chunk_id = uuid4()
    fake_chunk = Chunk(
        content="original",
        metadata=ChunkMetadata(
            source_file=src,
            scope="user",
            project_root=None,
            start_line=1,
            end_line=3,
        ),
        embedding=[0.1] * 1024,
    )
    monkeypatch.setattr(comp.storage, "get_chunk", AsyncMock(return_value=fake_chunk))

    # Stub the file mutation + reindex so the test stays at the gate
    # boundary (file IO happens in real bm25 storage, but with a
    # synthetic chunk the line-replace + reindex pipeline isn't useful).
    async def fake_index_file(*args, **kwargs):
        from memtomem.models import IndexingStats

        return IndexingStats(0, 0, 0, 0, 0, 0.0)

    monkeypatch.setattr(comp.index_engine, "index_file", fake_index_file)

    out = await memory_crud.mem_edit(
        chunk_id=str(chunk_id),
        new_content=_SECRET,
        force_unsafe=True,
        ctx=ctx,
    )
    # No project_shared error — user-scope chunks still allow bypass.
    assert "force_unsafe=True is not permitted" not in out
    snap = privacy.snapshot()
    assert snap["by_tool"]["mem_edit"]["bypassed"] == 1


# ---------------------------------------------------------------------------
# Scope-aware write target — MCP mem_add lands in the right tier directory
# (PR-D review #9: gate alone is not enough; metadata must persist scope.)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mem_add_project_shared_writes_to_project_dir_and_persists_metadata(
    bm25_only_components, monkeypatch, tmp_path
):
    """``mem_add(scope='project_shared', confirm=True)`` must:

    1. Land the file under ``<project>/.memtomem/memories/`` (not the
       user-tier ``memory_dirs[0]``).
    2. Persist ``metadata.scope == 'project_shared'`` and
       ``metadata.project_root == <project_root>`` on every indexed
       chunk so the read surface (PR-C ``ScopeFilter``) sees the
       correct tier.
    """
    comp, _user_mem_dir = bm25_only_components
    project_root = tmp_path / "proj_a"
    (project_root / ".memtomem" / "memories").mkdir(parents=True)
    # Register the project tier with the indexer so the scope classifier
    # tags chunks with project_shared on re-index.
    comp.config.indexing.project_memory_dirs = [project_root / ".memtomem" / "memories"]

    # Pin project_root resolution to this test's fixture (real
    # ``_resolve_project_context_root`` walks cwd; tmp_path may not
    # cover it portably).
    from memtomem.server.tools import memory_crud as _mc

    monkeypatch.setattr(
        "memtomem.server.tools.search._resolve_project_context_root",
        lambda app: project_root,
    )

    app = AppContext.from_components(comp)
    ctx = StubCtx(app)
    out = await _mc.mem_add(
        content="harmless team rule",
        scope="project_shared",
        confirm_project_shared=True,
        ctx=ctx,
    )
    assert "Memory added to" in out
    # File location pin.
    assert str(project_root / ".memtomem" / "memories") in out
    # Metadata pin: indexed chunks carry scope+project_root.
    chunks = await comp.storage.list_chunks_by_source(
        next((project_root / ".memtomem" / "memories").glob("*.md"))
    )
    assert chunks, "expected at least one chunk indexed under project_shared"
    for c in chunks:
        assert c.metadata.scope == "project_shared", c.metadata.scope
        assert c.metadata.project_root == project_root, c.metadata.project_root


@pytest.mark.asyncio
async def test_mem_add_project_shared_without_project_context_errors(
    bm25_only_components, monkeypatch
):
    """No registered project tier → ``scope='project_shared'`` errors clearly."""
    comp, _ = bm25_only_components
    # project_memory_dirs is empty by default in the fixture.
    monkeypatch.setattr(
        "memtomem.server.tools.search._resolve_project_context_root",
        lambda app: None,
    )
    app = AppContext.from_components(comp)
    ctx = StubCtx(app)
    out = await memory_crud.mem_add(
        content="rule",
        scope="project_shared",
        confirm_project_shared=True,
        ctx=ctx,
    )
    assert "Error" in out
    assert "registered project context" in out


@pytest.mark.asyncio
async def test_mem_batch_add_project_shared_writes_to_project_dir(
    bm25_only_components, monkeypatch, tmp_path
):
    """Batch path mirrors single-add scope-aware target dir."""
    comp, _ = bm25_only_components
    project_root = tmp_path / "proj_b"
    (project_root / ".memtomem" / "memories").mkdir(parents=True)
    comp.config.indexing.project_memory_dirs = [project_root / ".memtomem" / "memories"]
    monkeypatch.setattr(
        "memtomem.server.tools.search._resolve_project_context_root",
        lambda app: project_root,
    )

    app = AppContext.from_components(comp)
    ctx = StubCtx(app)
    out = await memory_crud.mem_batch_add(
        entries=[{"key": "k1", "value": "v1"}, {"key": "k2", "value": "v2"}],
        scope="project_shared",
        confirm_project_shared=True,
        ctx=ctx,
    )
    assert "Batch add complete" in out
    assert str(project_root / ".memtomem" / "memories") in out


# ---------------------------------------------------------------------------
# mem_consolidate_apply cross-scope rejection
# (PR-D review #4: chunk_ids is the truth source, not group["source"]
#  — protects against re-index / source rename between consolidate and apply.
#  PR-D review #5: skip is user-visible in the MCP return string,
#  not just a logger.warning.)
# ---------------------------------------------------------------------------


async def _stage_consolidate_group(
    comp,
    chunks: list[Chunk],
    group_id: int = 0,
    namespace: str | None = None,
    source_path: Path | None = None,
):
    """Insert ``chunks`` into storage and pre-stage a scratch group entry.

    Used by the consolidation tests below — bypasses the
    ``mem_consolidate`` discovery path so we can pin specific
    scope mixes per group.
    """
    import json
    from datetime import datetime, timedelta, timezone

    await comp.storage.upsert_chunks(chunks)
    group = {
        "group_id": group_id,
        "source": str(source_path or chunks[0].metadata.source_file),
        "chunk_count": len(chunks),
        "total_tokens": sum(len(c.content.split()) for c in chunks),
        "namespace": namespace,
        "previews": [],
        "chunk_ids": [str(c.id) for c in chunks],
    }
    expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds")
    await comp.storage.scratch_set(
        "consolidation_groups",
        json.dumps([group], default=str),
        expires_at=expires,
    )


def _fake_chunk(scope: str, source_file: Path, content: str = "x") -> Chunk:
    return Chunk(
        content=content,
        metadata=ChunkMetadata(
            source_file=source_file,
            scope=scope,
            project_root=source_file.parent.parent.parent if scope != "user" else None,
            start_line=1,
            end_line=2,
        ),
        embedding=[0.1] * 1024,
    )


@pytest.mark.asyncio
async def test_mem_consolidate_apply_mixed_scope_returns_skip_message(
    bm25_only_components, tmp_path
):
    """Mixed user + project_shared chunks → user-visible skip with reason."""
    from memtomem.server.tools import consolidation

    comp, mem_dir = bm25_only_components
    user_md = mem_dir / "user.md"
    user_md.write_text("## hi\n\nuser content\n")
    proj_root = tmp_path / "proj_mixed"
    proj_dir = proj_root / ".memtomem" / "memories"
    proj_dir.mkdir(parents=True)
    proj_md = proj_dir / "proj.md"
    proj_md.write_text("## hi\n\nproject content\n")

    chunks = [
        _fake_chunk("user", user_md, "user one"),
        _fake_chunk("project_shared", proj_md, "shared one"),
    ]
    await _stage_consolidate_group(comp, chunks, group_id=0)

    app = AppContext.from_components(comp)
    ctx = StubCtx(app)
    out = await consolidation.mem_consolidate_apply(
        group_id=0,
        summary="combined",
        ctx=ctx,
    )
    assert "skipped group 0" in out
    assert "mixed memory scopes" in out
    # Both scope names present so the caller knows which tiers conflict.
    assert "user" in out
    assert "project_shared" in out


@pytest.mark.asyncio
async def test_mem_consolidate_apply_project_shared_requires_confirm(
    bm25_only_components, tmp_path
):
    """All-project_shared group still rejects without explicit confirm."""
    from memtomem.server.tools import consolidation

    comp, _ = bm25_only_components
    proj_root = tmp_path / "proj_share"
    proj_dir = proj_root / ".memtomem" / "memories"
    proj_dir.mkdir(parents=True)
    src = proj_dir / "p.md"
    src.write_text("## hi\n\nproject content\n")

    chunks = [
        _fake_chunk("project_shared", src, "shared one"),
        _fake_chunk("project_shared", src, "shared two"),
    ]
    await _stage_consolidate_group(comp, chunks, group_id=0)

    app = AppContext.from_components(comp)
    ctx = StubCtx(app)
    out = await consolidation.mem_consolidate_apply(
        group_id=0,
        summary="combined",
        ctx=ctx,
    )
    assert "scope='project_shared'" in out
    assert "confirm_project_shared=True" in out


def test_validate_path_accepts_project_memory_dirs(tmp_path):
    """``_validate_path`` rejects absolute paths outside both base lists,
    accepts paths under either ``memory_dirs`` or ``project_memory_dirs``.
    """
    user_dir = tmp_path / "user_mem"
    user_dir.mkdir()
    project_dir = tmp_path / "proj" / ".memtomem" / "memories"
    project_dir.mkdir(parents=True)

    # Path under project_memory_dirs is accepted only when the helper
    # is told about that base.
    project_file = project_dir / "x.md"
    out, err = memory_crud._validate_path(str(project_file), [user_dir], None)
    assert err is not None  # rejected without project base
    out, err = memory_crud._validate_path(str(project_file), [user_dir], [project_dir])
    assert err is None
    assert out == project_file.resolve()

    # Outside-of-everything stays rejected.
    bogus = tmp_path / "elsewhere" / "y.md"
    bogus.parent.mkdir()
    bogus.write_text("x")
    out, err = memory_crud._validate_path(str(bogus), [user_dir], [project_dir])
    assert err is not None
