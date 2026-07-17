"""PR2 surface pins for the cross-process memory-CRUD lock (issue #1587):
the ``memtomem.tools.memory_mutation`` helpers and the CLI ``mm mem add`` path.

The web PATCH/DELETE/add pins live in ``test_web_routes.py``
(``TestChunkCrudCrossProcessLock``); these cover the surface-neutral helpers
directly plus the CLI add timeout surface.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock
from uuid import uuid4

import click
import pytest

from memtomem.context import _atomic
from memtomem.context._atomic import _lock_path_for, async_file_lock
from memtomem.models import IndexingStats
from memtomem.tools.memory_mutation import locked_source_chunk, mutate_source_and_reindex


def _stats() -> IndexingStats:
    return IndexingStats(
        total_files=1,
        total_chunks=1,
        indexed_chunks=1,
        skipped_chunks=0,
        deleted_chunks=0,
        duration_ms=1.0,
    )


# ------------------------------------------------------- locked_source_chunk


@pytest.mark.asyncio
async def test_locked_source_chunk_not_found():
    storage = AsyncMock()
    storage.get_chunk = AsyncMock(return_value=None)
    async with locked_source_chunk(storage, uuid4()) as (chunk, reason):
        assert chunk is None
        assert reason == "not_found"


@pytest.mark.asyncio
async def test_locked_source_chunk_times_out(tmp_path, monkeypatch):
    """A held sidecar makes the helper report ``"locked"`` within the budget."""
    from memtomem.models import Chunk, ChunkMetadata

    src = tmp_path / "n.md"
    src.write_text("## H\n\nbody\n", encoding="utf-8")
    chunk = Chunk(content="body", metadata=ChunkMetadata(source_file=src, start_line=1, end_line=3))
    storage = AsyncMock()
    storage.get_chunk = AsyncMock(return_value=chunk)
    monkeypatch.setattr(_atomic, "_CRUD_SIDECAR_LOCK_BUDGET_S", 0.2)

    async with async_file_lock(_lock_path_for(src.resolve()), timeout=5.0):
        async with locked_source_chunk(storage, uuid4()) as (fresh, reason):
            assert fresh is None
            assert reason == "locked"


@pytest.mark.asyncio
async def test_locked_source_chunk_propagates_body_timeout(tmp_path):
    """A ``TimeoutError`` raised by the caller's body after the lock is acquired
    must propagate, not be masked as a lock-acquire timeout (which would also
    break the @asynccontextmanager protocol with a second yield)."""
    from memtomem.models import Chunk, ChunkMetadata

    src = tmp_path / "n.md"
    src.write_text("body\n", encoding="utf-8")
    chunk = Chunk(content="body", metadata=ChunkMetadata(source_file=src, start_line=1, end_line=1))
    storage = AsyncMock()
    storage.get_chunk = AsyncMock(return_value=chunk)

    with pytest.raises(TimeoutError, match="from body"):
        async with locked_source_chunk(storage, uuid4()) as (fresh, reason):
            assert reason is None
            raise TimeoutError("from body")


# ------------------------------------------------- mutate_source_and_reindex


@pytest.mark.asyncio
async def test_mutate_source_and_reindex_success(tmp_path):
    src = tmp_path / "n.md"
    src.write_text("orig\n", encoding="utf-8")
    engine = AsyncMock()
    engine.index_file = AsyncMock(return_value=_stats())

    def mutate():
        src.write_text("mutated\n", encoding="utf-8")

    stats = await mutate_source_and_reindex(engine, src, mutate)
    assert stats.indexed_chunks == 1
    assert src.read_text(encoding="utf-8") == "mutated\n"
    # index_file was called with lock_held=True (caller owns the sidecar).
    assert engine.index_file.await_args.kwargs["lock_held"] is True
    assert not engine.index_file.await_args.kwargs.get("force", False)


@pytest.mark.asyncio
async def test_mutate_source_and_reindex_rolls_back_on_failure(tmp_path):
    src = tmp_path / "n.md"
    src.write_text("orig\n", encoding="utf-8")
    engine = AsyncMock()
    # Forward reindex raises; the rollback reindex (2nd call) succeeds.
    engine.index_file = AsyncMock(side_effect=[RuntimeError("boom"), _stats()])

    def mutate():
        src.write_text("mutated\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="boom"):
        await mutate_source_and_reindex(engine, src, mutate)
    # File restored to its pre-image; both the forward and rollback reindex ran.
    assert src.read_text(encoding="utf-8") == "orig\n"
    assert engine.index_file.await_count == 2
    assert all(not call.kwargs.get("force", False) for call in engine.index_file.await_args_list)


# ------------------------------------------------------------- CLI mm mem add


@pytest.mark.asyncio
async def test_cli_add_times_out_when_sidecar_held(bm25_only_components, monkeypatch):
    """``mm mem add`` raises a friendly ``ClickException`` (not a traceback) when
    another process holds the target file's sidecar."""
    from memtomem.cli import memory as cli_memory

    comp, mem_dir = bm25_only_components

    @asynccontextmanager
    async def _fake_components():
        yield comp

    monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _fake_components)
    monkeypatch.setattr(_atomic, "_CRUD_SIDECAR_LOCK_BUDGET_S", 0.2)

    target = (mem_dir / "notes.md").resolve()
    async with async_file_lock(_lock_path_for(target), timeout=5.0):
        with pytest.raises(click.ClickException) as excinfo:
            await cli_memory._add("hello world", None, [], "notes.md")
    assert "locked by another process" in str(excinfo.value)
