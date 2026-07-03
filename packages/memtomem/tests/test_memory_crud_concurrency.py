"""Concurrency pins for the MCP memory-CRUD write path (issue #1570).

``mem_edit`` / ``mem_delete`` rewrite a chunk's source markdown by line
range (``replace_chunk_body`` / ``remove_lines``) and re-index; ``mem_add``
appends and re-indexes. Before #1570 none of these serialized the
read → rewrite → re-index → rollback span, so two concurrent tool calls on
the same file could lose an update, splice over an unrelated entry with a
stale line range, or have one call's rollback erase another's committed
write. The fix holds ``AppContext.get_memory_file_lock(path)`` across each
span; these tests reproduce the corruption without the lock (a/b) and pin
the lock's behaviour (c/d).

Gate mechanics: ``replace_chunk_body`` runs inside ``asyncio.to_thread``, so
the interleave is driven with a ``threading.Event`` the worker blocks on
while the test coroutine advances the loop; ``index_file`` is async, so its
gate uses an ``asyncio.Event``.
"""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from helpers import StubCtx
from memtomem.models import Chunk, ChunkMetadata
from memtomem.server.context import AppContext
from memtomem.server.tools import memory_crud
from memtomem.tools import memory_writer


async def _chunks_by_start_line(comp, path):
    chunks = await comp.storage.list_chunks_by_source(path.resolve())
    return sorted(chunks, key=lambda c: c.metadata.start_line)


class TestMemEditConcurrency:
    @pytest.mark.asyncio
    async def test_stale_line_range_does_not_corrupt_sibling(
        self, bm25_only_components, monkeypatch
    ):
        """Two ``mem_edit`` calls on different chunks of one file: the second
        must not overwrite the first with a stale line range. Regression pin —
        fails before the per-file lock (the blocked edit resumes with a range
        that a concurrent growing edit already shifted).

        The earlier chunk (Alpha) is grown by +2 lines while the later chunk's
        (Beta) edit is parked mid-rewrite. Without the lock, Beta's resumed
        rewrite splices at its pre-shift range and mangles the file.
        """
        comp, mem_dir = bm25_only_components
        app = AppContext.from_components(comp)
        ctx = StubCtx(app)

        await memory_crud.mem_add(
            content="Alpha body original", title="Alpha", file="d.md", ctx=ctx
        )
        await memory_crud.mem_add(content="Beta body original", title="Beta", file="d.md", ctx=ctx)
        f = mem_dir / "d.md"
        alpha, beta = await _chunks_by_start_line(comp, f)

        entered = threading.Event()
        release = threading.Event()
        real_replace = memory_writer.replace_chunk_body
        seen: list[int] = []

        def gated_replace(path, start, end, new_content):
            first = not seen
            seen.append(1)
            if first:
                entered.set()
                release.wait(10)
            return real_replace(path, start, end, new_content)

        monkeypatch.setattr(memory_writer, "replace_chunk_body", gated_replace)

        # Beta's edit reaches the gated rewrite first and parks there.
        t_beta = asyncio.create_task(
            memory_crud.mem_edit(chunk_id=str(beta.id), new_content="BETA NEW BODY", ctx=ctx)
        )
        await asyncio.to_thread(entered.wait, 10)

        # Alpha grows by +2 lines (1 body line → 3), shifting Beta downward.
        t_alpha = asyncio.create_task(
            memory_crud.mem_edit(
                chunk_id=str(alpha.id), new_content="ALPHA NEW\nline2\nline3", ctx=ctx
            )
        )
        for _ in range(50):
            await asyncio.sleep(0)
        release.set()
        await asyncio.gather(t_alpha, t_beta)

        final = f.read_text(encoding="utf-8")
        assert final.count("## Alpha") == 1
        assert final.count("## Beta") == 1
        assert "line3" in final  # Alpha's grown body survived intact
        assert "BETA NEW BODY" in final
        assert "Alpha body original" not in final
        assert "Beta body original" not in final

    @pytest.mark.asyncio
    async def test_rollback_does_not_erase_concurrent_append(self, bm25_only_components):
        """An edit whose re-index fails rolls the file back to its own
        pre-image. A ``mem_add`` that committed during the edit's span must
        survive that rollback. Regression pin — fails before the per-file
        lock (the append lands mid-span and the rollback's ``write_text``
        erases it).
        """
        comp, mem_dir = bm25_only_components
        app = AppContext.from_components(comp)
        ctx = StubCtx(app)

        await memory_crud.mem_add(content="Alpha body", title="Alpha", file="d.md", ctx=ctx)
        f = mem_dir / "d.md"
        (alpha,) = await _chunks_by_start_line(comp, f)

        entered = asyncio.Event()
        release = asyncio.Event()
        real_index = app.index_engine.index_file
        force_calls = 0

        async def gated_index(path, *args, **kwargs):
            nonlocal force_calls
            if kwargs.get("force"):
                force_calls += 1
                if force_calls == 1:  # the edit's forward re-index
                    entered.set()
                    await release.wait()
                    raise RuntimeError("boom")
            return await real_index(path, *args, **kwargs)

        app.index_engine.index_file = gated_index  # type: ignore[method-assign]

        t_edit = asyncio.create_task(
            memory_crud.mem_edit(chunk_id=str(alpha.id), new_content="EDIT BODY", ctx=ctx)
        )
        await entered.wait()  # edit is parked before raising, holding the lock

        t_add = asyncio.create_task(
            memory_crud.mem_add(content="INJECTED", title="Injected", file="d.md", ctx=ctx)
        )
        # Pre-fix: the append lands on disk within a handful of iterations
        # (no lock). Post-fix: it is blocked on the lock, so INJECTED never
        # appears — the poll exhausts its small budget and release proceeds
        # anyway. Budget kept short (~200ms worst case) because exhausting
        # it is the expected steady-state on fixed code.
        for _ in range(40):
            if "INJECTED" in f.read_text(encoding="utf-8"):
                break
            await asyncio.sleep(0.005)
        release.set()
        out_edit, _ = await asyncio.gather(t_edit, t_add)

        assert "rolled back" in out_edit
        final = f.read_text(encoding="utf-8")
        assert "INJECTED" in final  # the concurrent append was not erased
        by_source = await comp.storage.list_chunks_by_source(f.resolve())
        assert any("INJECTED" in c.content for c in by_source)

    @pytest.mark.asyncio
    async def test_chunk_deleted_while_waiting_for_lock(self, bm25_only_components):
        """If a chunk is deleted while an edit waits on the per-file lock, the
        edit re-fetches fresh under the lock, sees it gone, and returns "not
        found" without touching the file.
        """
        comp, mem_dir = bm25_only_components
        app = AppContext.from_components(comp)
        ctx = StubCtx(app)

        await memory_crud.mem_add(content="Alpha body", title="Alpha", file="d.md", ctx=ctx)
        f = mem_dir / "d.md"
        (alpha,) = await _chunks_by_start_line(comp, f)

        lock = app.get_memory_file_lock(f)
        await lock.acquire()
        try:
            t_edit = asyncio.create_task(
                memory_crud.mem_edit(chunk_id=str(alpha.id), new_content="NEW", ctx=ctx)
            )
            for _ in range(20):  # let the edit prefetch and park on the lock
                await asyncio.sleep(0)
            await comp.storage.delete_by_source(f.resolve())
            f.write_text("", encoding="utf-8")
        finally:
            lock.release()

        out = await t_edit
        assert out == f"Error: chunk {alpha.id} not found."
        assert f.read_text(encoding="utf-8") == ""  # edit did not write


class TestBulkDeleteSerialization:
    """The bulk ``mem_delete`` branches (``source_file=`` / ``namespace=``)
    only remove index rows, so a concurrent locked CRUD span whose
    ``index_file(force=True)`` lands *after* the bulk delete re-upserts the
    whole file and silently resurrects the rows the delete just removed.
    Both branches must serialize on the same per-file lock(s).
    """

    @pytest.mark.asyncio
    async def test_bulk_source_delete_not_resurrected_by_inflight_edit(self, bm25_only_components):
        """``mem_delete(source_file=...)`` racing an in-flight ``mem_edit`` on
        the same file must order after the edit's re-index — final state has
        zero rows for the file. Regression pin — fails before the bulk branch
        takes the per-file lock (the delete lands mid-span and the edit's
        re-index resurrects every chunk).
        """
        comp, mem_dir = bm25_only_components
        app = AppContext.from_components(comp)
        ctx = StubCtx(app)

        await memory_crud.mem_add(content="Alpha body", title="Alpha", file="d.md", ctx=ctx)
        await memory_crud.mem_add(content="Beta body", title="Beta", file="d.md", ctx=ctx)
        f = mem_dir / "d.md"
        alpha, _beta = await _chunks_by_start_line(comp, f)

        entered = asyncio.Event()
        release = asyncio.Event()
        real_index = app.index_engine.index_file

        async def gated_index(path, *args, **kwargs):
            if kwargs.get("force") and not entered.is_set():
                entered.set()
                await release.wait()
            return await real_index(path, *args, **kwargs)

        app.index_engine.index_file = gated_index  # type: ignore[method-assign]

        t_edit = asyncio.create_task(
            memory_crud.mem_edit(chunk_id=str(alpha.id), new_content="EDITED", ctx=ctx)
        )
        await entered.wait()  # edit holds the file lock, parked inside re-index

        t_delete = asyncio.create_task(memory_crud.mem_delete(source_file=str(f), ctx=ctx))
        for _ in range(50):  # give the delete every chance to (wrongly) run mid-span
            await asyncio.sleep(0)
        release.set()
        out_edit, out_delete = await asyncio.gather(t_edit, t_delete)

        assert "Memory updated" in out_edit
        assert "Removed" in out_delete
        remaining = await comp.storage.list_chunks_by_source(f.resolve())
        assert remaining == []  # bulk delete was not resurrected by the edit

    @pytest.mark.asyncio
    async def test_bulk_namespace_delete_not_resurrected_by_inflight_add(
        self, bm25_only_components
    ):
        """``mem_delete(namespace=...)`` racing an in-flight ``mem_add`` into
        the same namespace/file must order after the add's re-index. Regression
        pin — fails before the namespace branch locks the namespace's source
        files.
        """
        comp, mem_dir = bm25_only_components
        app = AppContext.from_components(comp)
        ctx = StubCtx(app)

        await memory_crud.mem_add(
            content="Seed body", title="Seed", file="d.md", namespace="nsx", ctx=ctx
        )
        f = mem_dir / "d.md"

        entered = asyncio.Event()
        release = asyncio.Event()
        real_index = app.index_engine.index_file
        gate_armed = True  # seed add above already indexed; gate the next nsx call

        async def gated_index(path, *args, **kwargs):
            nonlocal gate_armed
            if gate_armed and kwargs.get("namespace") == "nsx":
                gate_armed = False
                entered.set()
                await release.wait()
            return await real_index(path, *args, **kwargs)

        app.index_engine.index_file = gated_index  # type: ignore[method-assign]

        t_add = asyncio.create_task(
            memory_crud.mem_add(
                content="Second body", title="Second", file="d.md", namespace="nsx", ctx=ctx
            )
        )
        await entered.wait()  # add holds the file lock, parked inside index

        t_delete = asyncio.create_task(memory_crud.mem_delete(namespace="nsx", ctx=ctx))
        for _ in range(50):
            await asyncio.sleep(0)
        release.set()
        _out_add, out_delete = await asyncio.gather(t_add, t_delete)

        assert "Removed" in out_delete
        remaining = await comp.storage.list_chunks_by_source(f.resolve())
        assert remaining == []  # namespace delete ordered after the add's index


class TestNamespaceResolutionUnderLock:
    @pytest.mark.asyncio
    async def test_namespace_resolved_at_write_time_not_lock_wait_time(self, bm25_only_components):
        """``mem_add`` without an explicit namespace derives it from the
        active agent session. The derivation must happen *after* the per-file
        lock is acquired: if the session changes while the add waits on the
        lock, the entry must land under the namespace active at write time,
        not a stale ``agent-runtime:<old-agent>`` captured before the wait.
        Regression pin for the lock refactor hoisting the resolution above
        the lock.
        """
        comp, mem_dir = bm25_only_components
        app = AppContext.from_components(comp)
        ctx = StubCtx(app)
        f = mem_dir / "d.md"

        app.current_agent_id = "alpha"  # active session → agent-runtime:alpha
        lock = app.get_memory_file_lock(f)
        await lock.acquire()
        try:
            t_add = asyncio.create_task(
                memory_crud.mem_add(content="Late body", title="Late", file="d.md", ctx=ctx)
            )
            for _ in range(20):  # let the add reach the lock wait
                await asyncio.sleep(0)
            app.current_agent_id = None  # session ends while the add waits
        finally:
            lock.release()

        out = await t_add
        assert "Memory added" in out
        (chunk,) = await comp.storage.list_chunks_by_source(f.resolve())
        assert chunk.metadata.namespace != "agent-runtime:alpha"


class TestLockKeyCanonicalization:
    @pytest.mark.asyncio
    async def test_case_variant_spellings_share_one_lock(self, bm25_only_components):
        """On case-insensitive filesystems (macOS APFS default, Windows NTFS)
        ``Notes.md`` and ``notes.md`` are the same file, so they must share one
        lock — otherwise the original #1570 corruption recurs across the two
        spellings. The key case-folds unconditionally: on case-sensitive
        filesystems that only makes two genuinely distinct files share a lock
        (needless serialization, never corruption).
        """
        comp, mem_dir = bm25_only_components
        app = AppContext.from_components(comp)
        assert app.get_memory_file_lock(mem_dir / "Notes.md") is app.get_memory_file_lock(
            mem_dir / "notes.md"
        )


class TestLockedChunkHelper:
    @pytest.mark.asyncio
    async def test_rekeys_onto_moved_file(self, bm25_only_components):
        """``_locked_chunk`` learns the path from an unlocked fetch, then
        re-fetches under the lock; if the file moved between the two fetches
        it re-keys onto the new path and holds that file's lock.
        """
        comp, tmp = bm25_only_components
        app = AppContext.from_components(comp)
        p1 = (tmp / "before.md").resolve()
        p2 = (tmp / "after.md").resolve()
        uid = uuid4()

        def _chunk(path):
            return Chunk(content="body", metadata=ChunkMetadata(source_file=path))

        # prefetch → p1, first locked re-fetch → p2 (moved), second → p2 (settled)
        app.storage.get_chunk = AsyncMock(  # type: ignore[method-assign]
            side_effect=[_chunk(p1), _chunk(p2), _chunk(p2)]
        )

        async with memory_crud._locked_chunk(app, uid, str(uid)) as (chunk, err):
            assert err is None
            assert chunk is not None
            assert chunk.metadata.source_file == p2
            assert app.get_memory_file_lock(p2).locked()
            assert not app.get_memory_file_lock(p1).locked()

    @pytest.mark.asyncio
    async def test_perpetually_moving_file_returns_retryable_error(self, bm25_only_components):
        """A chunk whose file keeps moving exhausts the bounded retry and
        returns a retryable error instead of looping forever.
        """
        comp, tmp = bm25_only_components
        app = AppContext.from_components(comp)
        uid = uuid4()

        def _chunk(n):
            return Chunk(
                content="body", metadata=ChunkMetadata(source_file=(tmp / f"m{n}.md").resolve())
            )

        # prefetch + one re-fetch per retry, each a new path → never settles.
        app.storage.get_chunk = AsyncMock(  # type: ignore[method-assign]
            side_effect=[_chunk(0), _chunk(1), _chunk(2), _chunk(3)]
        )

        async with memory_crud._locked_chunk(app, uid, str(uid)) as (chunk, err):
            assert chunk is None
            assert err is not None
            assert "being moved concurrently" in err
