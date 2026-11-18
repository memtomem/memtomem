"""A session summary's chunk id comes from ``new_chunk_ids`` (#3315).

The append callers moved to ``appended_chunk_ids`` (#3292) because re-indexing
a shared file also upserts a pending edit's chunk and misses a chunk that keeps
a stored id. Neither happens to a summary that this call indexes first: it
replaces a file named for its session alone, so the new ids are its chunks.
These tests pin that, including the case where the append attribution would
have lost the id (chunk overlap and a summary of several chunks leave it
``unverified``). The session holds the file's sidecar across its write and
its index, so an indexer that takes the sidecar cannot reach the file first
(#3340); one that skips it because the directory was missing still can (#3345).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import threading
import uuid

import pytest

from memtomem.context import _atomic
from memtomem.indexing.append_attribution import AppendRegion
from memtomem.indexing.redaction_exemption import indexer_text
from memtomem.server.context import AppContext
from memtomem.server.tools import session

pytestmark = pytest.mark.asyncio

_LONG = "\n\n".join(f"## Part {i}\n\n" + ("zqword " * 300) for i in range(4))


def _prepare(app: AppContext, summary: str):
    return session._prepare_summary_archive(
        app, session_id=str(uuid.uuid4()), agent_id=None, summary=summary, event_counts={}
    )


async def _rows(app: AppContext, prepared) -> list:
    rows = await app.storage.list_chunks_by_source(prepared.target, limit=None)
    return sorted(rows, key=lambda c: c.metadata.start_line)


async def _write(app: AppContext, prepared, monkeypatch):
    seen = []
    real = app.index_engine.index_file

    async def record(*args, **kwargs):
        stats = await real(*args, **kwargs)
        seen.append(stats)
        return stats

    monkeypatch.setattr(app.index_engine, "index_file", record)
    line, chunk_id = await session._write_summary_archive(app, prepared)
    (stats,) = seen
    return line, chunk_id, stats


@pytest.mark.parametrize("overlap", [0, 32])
@pytest.mark.parametrize(
    ("summary", "several"), [("zqshort summary body", False), (_LONG, True)], ids=["one", "several"]
)
async def test_the_new_ids_are_the_summary_in_file_order(
    bm25_only_components, monkeypatch, overlap, summary, several
):
    components, _ = bm25_only_components
    components.index_engine._config.chunk_overlap_tokens = overlap
    app = AppContext.from_components(components)
    prepared = _prepare(app, summary)

    line, chunk_id, stats = await _write(app, prepared, monkeypatch)

    rows = await _rows(app, prepared)
    assert (len(rows) > 1) is several
    assert list(stats.new_chunk_ids) == [r.id for r in rows]
    assert chunk_id == rows[0].id
    assert rows[0].metadata.start_line == 1
    assert {r.metadata.namespace for r in rows} == {prepared.namespace}
    assert line == f"- Summary chunk: {prepared.namespace} ({len(rows)} chunks)"


async def test_overlap_leaves_the_whole_file_attribution_unverified(bm25_only_components):
    """The contract #3315 suggested for this caller (the whole file as the
    expected text, region at 0) has no ids to give once overlap is on."""
    components, _ = bm25_only_components
    components.index_engine._config.chunk_overlap_tokens = 32
    app = AppContext.from_components(components)
    prepared = _prepare(app, _LONG)
    prepared.target.parent.mkdir(parents=True, exist_ok=True)
    prepared.target.write_bytes(prepared.content.encode("utf-8"))
    region = AppendRegion(
        expected_text=indexer_text(prepared.content.encode("utf-8"), errors="replace"), start=0
    )

    stats = await app.index_engine.index_file(
        prepared.target, namespace=prepared.namespace, append_region=region
    )

    rows = await _rows(app, prepared)
    assert len(rows) > 1
    assert stats.appended_attribution == "unverified"
    assert stats.appended_chunk_ids == ()
    assert stats.new_chunk_ids[0] == rows[0].id


@pytest.mark.parametrize("overlap", [0, 32])
@pytest.mark.parametrize("summary", ["zqshort summary body", _LONG], ids=["one", "several"])
async def test_an_indexer_arriving_after_the_write_waits_for_the_archive(
    bm25_only_components, monkeypatch, overlap, summary
):
    """#3340: another indexer that reaches the file between its write and the
    session's own index waits on the sidecar the session holds, so the rows
    land in the archive namespace and the summary gets its id.

    The competitor starts right after the write. The session resumes when the
    competitor either finishes (no lock: it indexes first) or is seen waiting
    on the held sidecar, so neither outcome depends on timing."""
    components, _ = bm25_only_components
    components.index_engine._config.chunk_overlap_tokens = overlap
    app = AppContext.from_components(components)
    prepared = _prepare(app, summary)
    real_index = app.index_engine.index_file
    real_sidecar = _atomic._sidecar_lock
    blocked = asyncio.Event()
    competitor: list[asyncio.Task] = []
    own: list = []

    @contextlib.asynccontextmanager
    async def sidecar(lock_path, **kwargs):
        if (
            competitor
            and asyncio.current_task() is competitor[0]
            and _atomic._intra_async_lock_for(lock_path).locked()
        ):
            blocked.set()
        async with real_sidecar(lock_path, **kwargs) as taken:
            yield taken

    async def index_file(path, *args, **kwargs):
        task = asyncio.create_task(real_index(path))
        competitor.append(task)
        waiter = asyncio.create_task(blocked.wait())
        await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
        waiter.cancel()
        stats = await real_index(path, *args, **kwargs)
        own.append(stats)
        return stats

    monkeypatch.setattr(_atomic, "_sidecar_lock", sidecar)
    monkeypatch.setattr(app.index_engine, "index_file", index_file)

    line, chunk_id = await session._write_summary_archive(app, prepared)
    (task,) = competitor
    late = await task

    rows = await _rows(app, prepared)
    assert {r.metadata.namespace for r in rows} == {prepared.namespace}
    (stats,) = own
    assert list(stats.new_chunk_ids) == [r.id for r in rows]
    assert chunk_id == rows[0].id
    assert line == f"- Summary chunk: {prepared.namespace} ({len(rows)} chunks)"
    assert late.indexed_chunks == 0
    assert [r.id for r in await _rows(app, prepared)] == [r.id for r in rows]
    assert blocked.is_set()

    hidden, _ = await app.search_pipeline.search("zqshort zqword", top_k=10)
    assert all(r.chunk.metadata.source_file != prepared.target for r in hidden)
    found, _ = await app.search_pipeline.search(
        "zqshort zqword", top_k=10, namespace=prepared.namespace
    )
    assert {r.chunk.id for r in found} & {r.id for r in rows}


@pytest.mark.requires_symlinks
async def test_a_path_replaced_after_the_write_is_not_indexed_through(
    bm25_only_components, monkeypatch
):
    """The index reads only the file the write placed: a symlink swapped in
    before the index is held, and its target is not indexed into the archive
    under the summary's lock."""
    components, _ = bm25_only_components
    app = AppContext.from_components(components)
    prepared = _prepare(app, "zqshort summary body")
    other = prepared.target.parents[2] / "unrelated.md"
    other.write_text("# Unrelated\n\nzqother body\n", encoding="utf-8")
    real_index = app.index_engine.index_file
    seen = []

    async def index_file(path, *args, **kwargs):
        link = path.with_name(".swap.md")
        link.symlink_to(other)
        os.replace(link, path)
        stats = await real_index(path, *args, **kwargs)
        seen.append(stats)
        return stats

    monkeypatch.setattr(app.index_engine, "index_file", index_file)

    assert await session._write_summary_archive(app, prepared) == (None, None)
    (stats,) = seen
    assert stats.relinked_files == 1
    assert await app.storage.list_chunks_by_source(other, limit=None) == []


async def test_a_cancel_keeps_the_sidecar_until_the_write_thread_is_done(
    bm25_only_components, monkeypatch
):
    """A cancelled session end waits for its write thread before it lets go of
    the sidecar, so no indexer can take the lock while the archive is still
    being written."""
    components, _ = bm25_only_components
    app = AppContext.from_components(components)
    prepared = _prepare(app, "zqshort summary body")
    real_write = _atomic.atomic_write_bytes
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = threading.Event()

    def write(*args, **kwargs):
        loop.call_soon_threadsafe(started.set)
        release.wait(timeout=30)
        return real_write(*args, **kwargs)

    monkeypatch.setattr(_atomic, "atomic_write_bytes", write)
    task = asyncio.create_task(session._write_summary_archive(app, prepared))
    await started.wait()
    lock = _atomic._intra_async_lock_for(_atomic.memory_lock_path(prepared.target))
    try:
        task.cancel()
        done, _ = await asyncio.wait({task}, timeout=0.2)
        assert not done
        assert lock.locked()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not lock.locked()
    assert prepared.target.read_text(encoding="utf-8") == prepared.content
