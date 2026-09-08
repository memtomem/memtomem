"""PR2 surface pins for the cross-process memory-CRUD lock (issue #1587):
the ``memtomem.tools.memory_mutation`` helpers and the CLI ``mm mem add`` path.

The web PATCH/DELETE/add pins live in ``test_web_routes.py``
(``TestChunkCrudCrossProcessLock``); these cover the surface-neutral helpers
directly plus the CLI add timeout surface.
"""

from __future__ import annotations

import logging
import shutil
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock
from uuid import uuid4

import click
import pytest

from memtomem.context import _atomic
from memtomem.context._atomic import (
    _lock_path_for,
    async_file_lock,
    async_memory_file_lock,
)
from memtomem.models import IndexingStats
from memtomem.tools import memory_mutation
from memtomem.tools.memory_mutation import locked_source_chunk, mutate_source_and_reindex
from memtomem.tools.memory_writer import RestoreOutcome, SourceRemovedError


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
    async with locked_source_chunk(storage, uuid4(), project_context_root=None) as (
        chunk,
        reason,
        _held,
    ):
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
        async with locked_source_chunk(storage, uuid4(), project_context_root=None) as (
            fresh,
            reason,
            _held,
        ):
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
        async with locked_source_chunk(storage, uuid4(), project_context_root=None) as (
            fresh,
            reason,
            _held,
        ):
            assert reason is None
            raise TimeoutError("from body")


@pytest.mark.asyncio
async def test_locked_source_chunk_does_not_resurrect_a_removed_parent(tmp_path):
    """#2346: a chunk outlives its source directory, and locking a delete must
    not bring that directory back.

    This is the surface with no L1 above it, so the in-process guard the span
    degrades to is its only serializer — asserted next door in
    ``test_locked_source_chunk_serializes_a_degraded_span``. Here the point is
    the footprint: the helper yields the chunk as it always did, and leaves
    nothing on disk.
    """
    from memtomem.models import Chunk, ChunkMetadata

    src = tmp_path / "gone" / "orphan.md"
    chunk = Chunk(content="body", metadata=ChunkMetadata(source_file=src, start_line=1, end_line=3))
    storage = AsyncMock()
    storage.get_chunk = AsyncMock(return_value=chunk)

    async with locked_source_chunk(storage, uuid4(), project_context_root=None) as (
        fresh,
        reason,
        _held,
    ):
        # The contract is unchanged: no fourth "source_gone" reason, because
        # every caller's next step already handles an absent source itself.
        assert reason is None
        assert fresh is chunk

    assert not (tmp_path / "gone").exists()
    assert not any(tmp_path.iterdir()), f"stray artifacts: {list(tmp_path.iterdir())}"


@pytest.mark.asyncio
async def test_locked_source_chunk_serializes_a_degraded_span(tmp_path, monkeypatch):
    """The degraded span still reports ``"locked"`` under contention.

    Two things at once, and both are needed. That a second caller is excluded
    at all — otherwise the span on a vanished source is a lock in name only.
    And that the exclusion surfaces through the SAME ``except TimeoutError``
    arm as a held sidecar, so the web routes keep answering 503 instead of
    letting a bare ``TimeoutError`` escape as a 500.
    """
    from memtomem.models import Chunk, ChunkMetadata

    src = tmp_path / "gone" / "orphan.md"
    chunk = Chunk(content="body", metadata=ChunkMetadata(source_file=src, start_line=1, end_line=3))
    storage = AsyncMock()
    storage.get_chunk = AsyncMock(return_value=chunk)
    monkeypatch.setattr(_atomic, "_CRUD_SIDECAR_LOCK_BUDGET_S", 0.2)

    async with async_memory_file_lock(src, timeout=5.0):
        async with locked_source_chunk(storage, uuid4(), project_context_root=None) as (
            fresh,
            reason,
            _held,
        ):
            assert fresh is None
            assert reason == "locked"

    assert not (tmp_path / "gone").exists()


@pytest.mark.asyncio
async def test_locked_source_chunk_reports_that_it_holds_no_cross_process_lock(tmp_path):
    """The third element is the whole basis of #2346's byte-write rule.

    The helper does not police the rule itself: a delete of index rows under a
    degraded span is fine, an edit of the file is not, and only the surface
    knows which it is doing. What the helper owes them is an honest answer
    about the lock it actually took — asserted on both branches, so a helper
    that hard-coded either value could not pass.
    """
    from memtomem.models import Chunk, ChunkMetadata

    gone = tmp_path / "gone" / "orphan.md"
    chunk = Chunk(
        content="body", metadata=ChunkMetadata(source_file=gone, start_line=1, end_line=3)
    )
    storage = AsyncMock()
    storage.get_chunk = AsyncMock(return_value=chunk)

    async with locked_source_chunk(storage, uuid4(), project_context_root=None) as (
        fresh,
        reason,
        cross_process_held,
    ):
        assert reason is None
        assert fresh is chunk
        assert cross_process_held is False

    live = tmp_path / "live" / "n.md"
    live.parent.mkdir()
    live.write_text("## H\n\nbody\n", encoding="utf-8")
    chunk = Chunk(
        content="body", metadata=ChunkMetadata(source_file=live, start_line=1, end_line=3)
    )
    storage.get_chunk = AsyncMock(return_value=chunk)

    async with locked_source_chunk(storage, uuid4(), project_context_root=None) as (
        fresh,
        reason,
        cross_process_held,
    ):
        assert reason is None
        assert cross_process_held is True


@pytest.mark.asyncio
async def test_locked_source_chunk_reports_no_lock_on_every_refusal(tmp_path, monkeypatch):
    """A refusal must not read as "you hold the flock".

    ``locked`` yields no chunk, so nothing acts on the bit today — but the
    default a caller would destructure is the dangerous one, and the next
    surface to consult it should not have to check which reasons are safe.
    """
    from memtomem.models import Chunk, ChunkMetadata

    storage = AsyncMock()
    storage.get_chunk = AsyncMock(return_value=None)
    async with locked_source_chunk(storage, uuid4(), project_context_root=None) as (
        _chunk,
        reason,
        cross_process_held,
    ):
        assert reason == "not_found"
        assert cross_process_held is False

    src = tmp_path / "n.md"
    src.write_text("## H\n\nbody\n", encoding="utf-8")
    chunk = Chunk(content="body", metadata=ChunkMetadata(source_file=src, start_line=1, end_line=3))
    storage.get_chunk = AsyncMock(return_value=chunk)
    monkeypatch.setattr(_atomic, "_CRUD_SIDECAR_LOCK_BUDGET_S", 0.2)

    async with async_file_lock(_lock_path_for(src.resolve()), timeout=5.0):
        async with locked_source_chunk(storage, uuid4(), project_context_root=None) as (
            _chunk,
            reason,
            cross_process_held,
        ):
            assert reason == "locked"
            assert cross_process_held is False


# ------------------------------------------------- mutate_source_and_reindex


@pytest.mark.asyncio
async def test_mutate_source_and_reindex_success(tmp_path):
    src = tmp_path / "n.md"
    src.write_text("orig\n", encoding="utf-8")
    engine = AsyncMock()
    engine.index_file = AsyncMock(return_value=_stats())

    def mutate(_pre):
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

    def mutate(_pre):
        src.write_text("mutated\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="boom"):
        await mutate_source_and_reindex(engine, src, mutate)
    # File restored to its pre-image; both the forward and rollback reindex ran.
    assert src.read_text(encoding="utf-8") == "orig\n"
    assert engine.index_file.await_count == 2
    assert all(not call.kwargs.get("force", False) for call in engine.index_file.await_args_list)


# The #2347 half: the rollback must not recreate a source another process
# removed, and its own failure must not replace the error it is rolling back.


@pytest.mark.asyncio
async def test_mutate_source_and_reindex_does_not_recreate_a_source_removed_mid_span(tmp_path):
    src = tmp_path / "n.md"
    src.write_text("orig\n", encoding="utf-8")
    engine = AsyncMock()

    async def index_file(path, **kwargs):
        if engine.index_file.await_count == 1:
            # An outside ``rm`` lands between the pre-image read and the
            # failure; the sidecar never bound it.
            path.unlink()
            raise RuntimeError("boom")
        return _stats()

    engine.index_file = AsyncMock(side_effect=index_file)

    def mutate(_pre):
        src.write_text("mutated\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="boom"):
        await mutate_source_and_reindex(engine, src, mutate)
    assert not src.exists()
    assert engine.index_file.await_count == 2


@pytest.mark.asyncio
async def test_mutate_source_and_reindex_reports_the_body_error_when_the_parent_is_gone(
    tmp_path, caplog
):
    holder = tmp_path / "memories"
    holder.mkdir()
    src = holder / "n.md"
    src.write_text("orig\n", encoding="utf-8")
    engine = AsyncMock()
    engine.index_file = AsyncMock(side_effect=[RuntimeError("boom"), _stats()])

    def mutate(_pre):
        src.write_text("mutated\n", encoding="utf-8")
        shutil.rmtree(holder)

    # Pre-#2347 the restore's own FileNotFoundError arrived here instead, with
    # the real cause demoted to ``__context__``.
    with caplog.at_level(logging.WARNING, logger="memtomem.tools.memory_mutation"):
        with pytest.raises(RuntimeError, match="boom"):
            await mutate_source_and_reindex(engine, src, mutate)
    assert not holder.exists()
    assert any(str(src) in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_mutate_source_and_reindex_still_raises_the_body_error_when_the_restore_fails(
    tmp_path, monkeypatch, caplog
):
    src = tmp_path / "n.md"
    src.write_text("orig\n", encoding="utf-8")
    engine = AsyncMock()
    engine.index_file = AsyncMock(side_effect=[RuntimeError("boom"), _stats()])
    monkeypatch.setattr(
        memory_mutation, "restore_pre_image_quietly", lambda *_: RestoreOutcome.failed
    )

    def mutate(_pre):
        src.write_text("mutated\n", encoding="utf-8")

    with caplog.at_level(logging.ERROR, logger="memtomem.tools.memory_mutation"):
        with pytest.raises(RuntimeError, match="boom"):
            await mutate_source_and_reindex(engine, src, mutate)
    assert any(str(src) in record.getMessage() for record in caplog.records)


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


@pytest.mark.asyncio
async def test_mutate_source_and_reindex_hands_the_pre_image_to_the_callback(tmp_path):
    """#2367: the callback needs the identity the span already read.

    Without it the write can only ask "does something exist at this path",
    which is a different question from "is this still the file I read".
    """
    src = tmp_path / "n.md"
    src.write_text("orig\n", encoding="utf-8")
    info = src.stat()
    engine = AsyncMock()
    engine.index_file = AsyncMock(return_value=_stats())
    seen = {}

    def mutate(pre):
        seen["identity"] = pre.identity
        seen["data"] = pre.data

    await mutate_source_and_reindex(engine, src, mutate)

    assert seen["identity"] == (info.st_dev, info.st_ino)
    assert seen["data"] == b"orig\n"


@pytest.mark.asyncio
async def test_a_refused_write_is_re_raised_without_a_restore(tmp_path, monkeypatch):
    """A write that refused wrote nothing, so there is nothing to put back.

    Restoring anyway would recreate the removed file wherever the filesystem
    cannot answer identity, since the restore falls back to existence there —
    the resurrection the refusal exists to prevent (#2367).
    """
    src = tmp_path / "n.md"
    src.write_text("orig\n", encoding="utf-8")
    engine = AsyncMock()
    engine.index_file = AsyncMock(return_value=_stats())
    restores: list[int] = []
    monkeypatch.setattr(
        memory_mutation,
        "restore_pre_image_quietly",
        lambda *_: restores.append(1) or RestoreOutcome.restored,
    )

    def mutate(_pre):
        src.unlink()
        raise SourceRemovedError("gone")

    with pytest.raises(SourceRemovedError):
        await mutate_source_and_reindex(engine, src, mutate)

    assert restores == []
    assert not src.exists()
    # The rollback re-index still ran; only the file restore was skipped.
    assert engine.index_file.await_count == 1


@pytest.mark.asyncio
async def test_a_refusal_after_the_write_landed_is_still_rolled_back(tmp_path):
    """The exemption is about *when*, not about the exception's type.

    This handler also covers the re-index, so a ``SourceChangedError`` arriving
    from a later stage would name a write that already happened. Skipping the
    rollback there would leave that write on disk (#2367 review).
    """
    src = tmp_path / "n.md"
    src.write_text("orig\n", encoding="utf-8")
    engine = AsyncMock()
    engine.index_file = AsyncMock(side_effect=[SourceRemovedError("late"), _stats()])

    def mutate(_pre):
        src.write_text("mutated\n", encoding="utf-8")

    with pytest.raises(SourceRemovedError):
        await mutate_source_and_reindex(engine, src, mutate)

    assert src.read_text(encoding="utf-8") == "orig\n"  # restored, not skipped
