"""Gate B judges the row the write acts on, not a pool connection's old snapshot (#3202).

A pool connection another thread still has a statement open on keeps serving
the snapshot from before later commits. Every case here holds such a statement,
commits ``scope='project_shared'`` for a row the snapshot still shows as
``user``, and then asks a surface to change the row without
``confirm_project_shared=True``. A gate that read the pool would see ``user``
and let the delete or edit through.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import UUID

import pytest

from helpers import StubCtx
from memtomem.models import Chunk, ChunkMetadata
from memtomem.server.context import AppContext
from memtomem.server.tools import memory_crud
from memtomem.source_provenance import source_span_hash
from test_storage_read_routing import pool_reader_mid_statement

_TEXT = "## team rule\n\noriginal body\n"
_NAMESPACE = "team"


async def _seed_shared_row_seen_as_user(comp, tmp_path: Path, monkeypatch) -> tuple[Chunk, Path]:
    """One row in the project's shared tier, committed as ``user`` for now."""
    project_root = tmp_path / "proj"
    proj_dir = project_root / ".memtomem" / "memories"
    proj_dir.mkdir(parents=True)
    source = proj_dir / "rule.md"
    source.write_text(_TEXT, encoding="utf-8")
    comp.config.indexing.project_memory_dirs = [proj_dir]
    monkeypatch.setattr(
        "memtomem.server.tools.search._resolve_project_context_root",
        lambda _app: project_root,
    )
    chunk = Chunk(
        content="original body",
        metadata=ChunkMetadata(
            source_file=source,
            namespace=_NAMESPACE,
            scope="project_shared",
            project_root=project_root,
            start_line=1,
            end_line=3,
            source_span_hash=source_span_hash(_TEXT.splitlines(), 1, 3),
        ),
        embedding=[0.1] * 1024,
    )
    await comp.storage.upsert_chunks([chunk])
    _commit_scope(comp.storage, chunk.id, "user")
    return chunk, source


def _commit_scope(storage, chunk_id: UUID, scope: str) -> None:
    writer = storage._get_db()
    writer.execute("UPDATE chunks SET scope=? WHERE id=?", (scope, str(chunk_id)))
    writer.commit()


def _row_count(storage, chunk_id: UUID) -> int:
    row = storage._get_db().execute("SELECT COUNT(*) FROM chunks WHERE id=?", (str(chunk_id),))
    return int(row.fetchone()[0])


async def _call(surface: str, chunk: Chunk, source: Path, ctx) -> str:
    if surface == "source_file":
        return await memory_crud.mem_delete(source_file=str(source), ctx=ctx)
    if surface == "namespace":
        return await memory_crud.mem_delete(namespace=_NAMESPACE, ctx=ctx)
    if surface == "chunk_id":
        return await memory_crud.mem_delete(chunk_id=str(chunk.id), ctx=ctx)
    assert surface == "mem_edit"
    return await memory_crud.mem_edit(chunk_id=str(chunk.id), new_content="rewritten body", ctx=ctx)


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["source_file", "namespace", "chunk_id", "mem_edit"])
async def test_mcp_gate_b_reads_past_an_open_pool_statement(
    surface, bm25_only_components, tmp_path, monkeypatch
):
    comp, _mem_dir = bm25_only_components
    chunk, source = await _seed_shared_row_seen_as_user(comp, tmp_path, monkeypatch)
    ctx = StubCtx(AppContext.from_components(comp))

    with pool_reader_mid_statement(comp.storage, monkeypatch):
        _commit_scope(comp.storage, chunk.id, "project_shared")
        # The pool still shows the row as it was: the precondition of the race.
        pooled = await comp.storage.get_chunk(chunk.id)
        assert pooled is not None and pooled.metadata.scope == "user"
        out = await _call(surface, chunk, source, ctx)

    assert "confirm_project_shared=True" in out
    assert _row_count(comp.storage, chunk.id) == 1
    assert source.read_text(encoding="utf-8") == _TEXT


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["source_file", "namespace", "chunk_id", "mem_edit"])
async def test_mcp_gate_b_busy_writer_is_a_retryable_refusal(
    surface, bm25_only_components, tmp_path, monkeypatch
):
    """Another task holding the writer past the wait refuses, retryably, changing nothing."""
    comp, _mem_dir = bm25_only_components
    chunk, source = await _seed_shared_row_seen_as_user(comp, tmp_path, monkeypatch)
    _commit_scope(comp.storage, chunk.id, "project_shared")
    ctx = StubCtx(AppContext.from_components(comp))
    # ``raising=False``: on a revision without the wait the test must fail on
    # the response, not on a missing attribute.
    monkeypatch.setattr("memtomem.server.tools.memory_crud.WRITER_WAIT_S", 0.05, raising=False)
    monkeypatch.setattr("memtomem.storage.sqlite_backend.WRITER_WAIT_S", 0.05)

    owner_ready = asyncio.Event()
    release = asyncio.Event()

    async def owner() -> None:
        async with comp.storage.transaction():
            owner_ready.set()
            await release.wait()

    task = asyncio.create_task(owner())
    await owner_ready.wait()
    try:
        out = await _call(surface, chunk, source, ctx)
    finally:
        release.set()
        await task

    assert out == "Error (retryable): the store's writer is busy; nothing was changed. Retry."
    assert _row_count(comp.storage, chunk.id) == 1
    assert source.read_text(encoding="utf-8") == _TEXT
