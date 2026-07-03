"""Cross-process serialization of the memory-CRUD read-modify-write span
(issue #1587, follow-up to #1570).

#1570 held an in-process ``asyncio.Lock`` across each CRUD span — enough for
several agents sharing ONE MCP server, but a second server, the CLI, or
``memory-migrate`` still raced the read→rewrite→reindex window. #1587 holds the
cross-process sidecar (``async_file_lock``, level L2 of the lock order in
``context._atomic``) across the whole span too, and hoists the engine's own
sidecar acquire above ``_index_lock`` so a CRUD caller can reach ``index_file``
with ``lock_held=True`` instead of self-deadlocking.

Test groups:
* **A** — cross-process (``multiprocessing`` spawn, like ``test_config_write_lock``):
  the sidecar serializes real appends across processes; times out cleanly.
* **B** — lock ordering: the sidecar is acquired while ``_index_lock`` is free;
  ``lock_held=True`` and the #1566 parent-gone case skip it.
* **C** — timeout surfacing: a held sidecar makes a CRUD span / migrate return a
  friendly retryable error rather than blocking (also pins the Windows-safe
  in-process guard — two contenders on one loop serialize via layer 1).
* **D** — watcher: a timed-out reindex is re-queued, not dropped.
* **E** — sidecar-lockfile hygiene: ``.*.lock`` files are never indexed.
"""

from __future__ import annotations

import asyncio
import multiprocessing as mp
import time
from pathlib import Path

import pytest

from helpers import StubCtx
from memtomem.context import _atomic as atomic_mod
from memtomem.context._atomic import _lock_path_for, async_file_lock
from memtomem.server.context import AppContext
from memtomem.server.tools import memory_crud

# spawn: uniform semantics across the CI matrix (Windows/macOS default), and the
# only context that genuinely gives distinct-process flock contention.
_CTX = mp.get_context("spawn")


# ----------------------------------------------------------------- helpers


def _locked_append(md_path_str: str, entry: str, q) -> None:
    """Locked read→append→write of one distinct line under ``async_file_lock``,
    with a widened read→write window so an unlocked version would reliably lose
    updates (mirrors ``test_config_write_lock._locked_add_section``). Runs in a
    child process, so serialization here comes purely from the cross-process
    flock — the in-process guard is per-process and cannot help across them."""

    async def run() -> None:
        path = Path(md_path_str)
        async with async_file_lock(_lock_path_for(path), timeout=20.0):
            existing = path.read_text(encoding="utf-8") if path.exists() else ""
            await asyncio.sleep(0.02)
            path.write_text(existing + entry + "\n", encoding="utf-8")

    asyncio.run(run())
    q.put(entry)


def _hold_sidecar(lock_path_str: str, ready_q, release_evt) -> None:
    """Hold the sidecar until signalled — stands in for another process owning
    the memory file's lock so same-file writers must time out."""

    async def run() -> None:
        async with async_file_lock(Path(lock_path_str), timeout=20.0):
            ready_q.put("acquired")
            await asyncio.to_thread(release_evt.wait, 30)

    asyncio.run(run())


# ============================================================ A. cross-process


def test_cross_process_appends_do_not_lose_updates(tmp_path: Path):
    """Positive pin: 8 processes each append a distinct line under the sidecar;
    all 8 survive. Without the lock the widened window loses updates."""
    md = tmp_path / "notes.md"
    entries = [f"entry-{i}" for i in range(8)]

    procs, queues = [], []
    for entry in entries:
        q = _CTX.Queue()
        p = _CTX.Process(target=_locked_append, args=(str(md), entry, q))
        queues.append(q)
        procs.append(p)
        p.start()

    for q in queues:
        assert q.get(timeout=30) in entries
    for p in procs:
        p.join(timeout=15)
        assert p.exitcode == 0

    survived = set(md.read_text(encoding="utf-8").split())
    assert survived == set(entries), (
        f"lost updates: expected all of {entries}, got {sorted(survived)}"
    )


def test_async_file_lock_uses_dot_prefixed_sidecar(tmp_path: Path):
    """The lock is a ``.{name}.lock`` sibling, never the data file — locking the
    data file wouldn't survive the ``os.replace`` inode swap."""

    async def run() -> None:
        md = tmp_path / "notes.md"
        async with async_file_lock(_lock_path_for(md), timeout=5.0):
            pass
        assert (tmp_path / ".notes.md.lock").exists()

    asyncio.run(run())


def test_async_file_lock_times_out_when_held_by_another_process(tmp_path: Path):
    """A separate process holding the sidecar makes an acquire raise
    ``TimeoutError`` (acquiring nothing) within the budget."""
    md = tmp_path / "notes.md"
    lock_path = _lock_path_for(md)

    ready_q = _CTX.Queue()
    release_evt = _CTX.Event()
    holder = _CTX.Process(target=_hold_sidecar, args=(str(lock_path), ready_q, release_evt))
    holder.start()
    try:
        assert ready_q.get(timeout=15) == "acquired"

        async def run() -> None:
            start = time.monotonic()
            with pytest.raises(TimeoutError):
                async with async_file_lock(lock_path, timeout=0.3):
                    pass
            # Bounded: gave up near the budget, did not hang on the holder.
            assert time.monotonic() - start < 5.0

        asyncio.run(run())
    finally:
        release_evt.set()
        holder.join(timeout=10)
        assert holder.exitcode == 0


# ============================================================ B. lock ordering


@pytest.mark.asyncio
async def test_index_file_acquires_sidecar_while_index_lock_free(bm25_only_components, monkeypatch):
    """The sidecar (L2) is acquired ABOVE ``_index_lock`` (L3): when the spy
    records the sidecar acquire, ``_index_lock`` must still be free. This is the
    reorder that removes the reverse-order cycle #1587 fixes."""
    comp, mem_dir = bm25_only_components
    src = mem_dir / "rule.md"
    src.write_text("## Rule\n\nbody.\n", encoding="utf-8")

    engine = comp.index_engine
    index_lock_held_at_acquire: list[bool] = []
    real = atomic_mod.async_file_lock

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def spy(lock_path, *, timeout):
        index_lock_held_at_acquire.append(engine._index_lock.locked())
        async with real(lock_path, timeout=timeout):
            yield

    monkeypatch.setattr(atomic_mod, "async_file_lock", spy)
    await engine.index_file(src.resolve())

    assert index_lock_held_at_acquire == [False], (
        "sidecar must be taken before _index_lock (L2 → L3), never while it is held"
    )


@pytest.mark.asyncio
async def test_index_file_lock_held_skips_sidecar(bm25_only_components, monkeypatch):
    """``lock_held=True`` skips the sidecar acquire entirely — the CRUD caller
    already holds it, and re-acquiring would self-deadlock."""
    comp, mem_dir = bm25_only_components
    src = mem_dir / "rule.md"
    src.write_text("## Rule\n\nbody.\n", encoding="utf-8")

    calls: list[Path] = []
    real = atomic_mod.async_file_lock

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def spy(lock_path, *, timeout):
        calls.append(lock_path)
        async with real(lock_path, timeout=timeout):
            yield

    monkeypatch.setattr(atomic_mod, "async_file_lock", spy)
    await comp.index_engine.index_file(src.resolve(), force=True, lock_held=True)

    assert calls == [], "lock_held=True must not acquire the sidecar"


@pytest.mark.asyncio
async def test_index_file_parent_gone_skips_sidecar_without_mkdir(
    bm25_only_components, monkeypatch, tmp_path
):
    """#1566: when the parent dir is gone, the sidecar is skipped so we never
    ``mkdir``-resurrect the directory the user removed."""
    comp, _mem_dir = bm25_only_components
    missing = tmp_path / "gone" / "orphan.md"  # parent 'gone/' never created

    calls: list[Path] = []
    real = atomic_mod.async_file_lock

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def spy(lock_path, *, timeout):
        calls.append(lock_path)
        async with real(lock_path, timeout=timeout):
            yield

    monkeypatch.setattr(atomic_mod, "async_file_lock", spy)
    await comp.index_engine.index_file(missing)  # delete-by-source pass

    assert calls == [], "parent-gone path must skip the sidecar"
    assert not (tmp_path / "gone").exists(), "sidecar acquire resurrected the deleted parent dir"


@pytest.mark.asyncio
async def test_stream_acquires_sidecar_while_index_lock_free(bm25_only_components, monkeypatch):
    """``index_path_stream`` uses the same L2 → L3 order as ``index_file``
    (via ``_index_file_locked``): the sidecar acquire happens while
    ``_index_lock`` is free (#1574 item 6). Before that fix the stream
    bypassed both locks entirely — ``calls`` would be empty."""
    comp, mem_dir = bm25_only_components
    src = mem_dir / "rule.md"
    src.write_text("## Rule\n\nbody.\n", encoding="utf-8")

    engine = comp.index_engine
    index_lock_held_at_acquire: list[bool] = []
    real = atomic_mod.async_file_lock

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def spy(lock_path, *, timeout):
        index_lock_held_at_acquire.append(engine._index_lock.locked())
        async with real(lock_path, timeout=timeout):
            yield

    monkeypatch.setattr(atomic_mod, "async_file_lock", spy)
    async for _event in engine.index_path_stream(src, recursive=False):
        pass

    assert index_lock_held_at_acquire == [False], (
        "the stream must take the sidecar (L2) before _index_lock (L3), once per file"
    )


@pytest.mark.asyncio
async def test_stream_sidecar_timeout_folds_into_errors_and_continues(
    bm25_only_components, monkeypatch
):
    """A held sidecar on one file must not abort the stream: that file's
    ``TimeoutError`` lands in ``complete.errors`` (no new event type) and
    the remaining files still index (#1574 item 6)."""
    comp, mem_dir = bm25_only_components
    stuck = mem_dir / "a-stuck.md"
    ok = mem_dir / "b-ok.md"
    stuck.write_text("## Stuck\n\nheld.\n", encoding="utf-8")
    ok.write_text("## Ok\n\nfree.\n", encoding="utf-8")

    monkeypatch.setattr(atomic_mod, "_MEMORY_SIDECAR_LOCK_BUDGET_S", 0.2)

    held = asyncio.Event()
    release = asyncio.Event()

    async def holder() -> None:
        async with async_file_lock(_lock_path_for(stuck.resolve()), timeout=5.0):
            held.set()
            await release.wait()

    holder_task = asyncio.create_task(holder())
    await held.wait()
    try:
        events = [e async for e in comp.index_engine.index_path_stream(mem_dir, recursive=True)]
    finally:
        release.set()
        await holder_task

    complete = next(e for e in events if e["type"] == "complete")
    assert len(complete["errors"]) == 1, f"expected 1 timeout error, got {complete['errors']}"
    assert "a-stuck.md" in complete["errors"][0]
    # The other file still indexed — the stream continued past the timeout.
    sources = {p.name for p in await comp.storage.get_all_source_files()}
    assert "b-ok.md" in sources
    assert "a-stuck.md" not in sources


# ============================================================ C. timeout surface


@pytest.mark.asyncio
async def test_mem_edit_times_out_when_sidecar_held(bm25_only_components, monkeypatch):
    """A held sidecar makes ``mem_edit`` return a friendly retryable error
    instead of blocking. Holding the lock on THIS loop also exercises the
    in-process (layer-1) guard that keeps same-process handlers serialized on
    Windows, where the flock alone would not (Codex #1587 review)."""
    comp, mem_dir = bm25_only_components
    app = AppContext.from_components(comp)
    ctx = StubCtx(app)

    await memory_crud.mem_add(content="Alpha body", title="Alpha", file="d.md", ctx=ctx)
    f = mem_dir / "d.md"
    (alpha,) = sorted(
        await comp.storage.list_chunks_by_source(f.resolve()),
        key=lambda c: c.metadata.start_line,
    )

    monkeypatch.setattr(atomic_mod, "_CRUD_SIDECAR_LOCK_BUDGET_S", 0.2)
    sidecar = _lock_path_for(f.resolve())

    async with async_file_lock(sidecar, timeout=5.0):
        out = await memory_crud.mem_edit(chunk_id=str(alpha.id), new_content="NEW BODY", ctx=ctx)

    assert "locked by another process" in out
    # File untouched — the edit never ran.
    assert "Alpha body" in f.read_text(encoding="utf-8")
    assert "NEW BODY" not in f.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_mem_add_times_out_when_sidecar_held(bm25_only_components, monkeypatch):
    """``mem_add`` surfaces the same retryable error under a held sidecar."""
    comp, mem_dir = bm25_only_components
    app = AppContext.from_components(comp)
    ctx = StubCtx(app)

    monkeypatch.setattr(atomic_mod, "_CRUD_SIDECAR_LOCK_BUDGET_S", 0.2)
    target = (mem_dir / "d.md").resolve()
    (mem_dir / "d.md").write_text("## Seed\n\nseed.\n", encoding="utf-8")

    async with async_file_lock(_lock_path_for(target), timeout=5.0):
        out = await memory_crud.mem_add(content="new entry", title="New", file="d.md", ctx=ctx)

    assert "locked by another process" in out


# ============================================================ D. watcher requeue


@pytest.mark.asyncio
async def test_watcher_reindex_returns_path_on_timeout(monkeypatch):
    """``_reindex`` returns the path (not ``None``) when the reindex times out
    on the sidecar, so the caller can retry it — the change is not lost."""
    from memtomem.config import IndexingConfig
    from memtomem.indexing.watcher import FileWatcher

    class _Engine:
        async def index_file(self, path):
            raise TimeoutError("sidecar held")

    watcher = FileWatcher(_Engine(), IndexingConfig(memory_dirs=[]))
    result = await watcher._reindex(Path("/some/notes.md"))
    assert result == Path("/some/notes.md")


@pytest.mark.asyncio
async def test_watcher_flush_batch_requeues_only_timed_out(monkeypatch):
    """``_flush_batch`` returns exactly the files whose reindex timed out (to be
    retried next window) and drops the ones that succeeded."""
    from memtomem.config import IndexingConfig
    from memtomem.indexing.watcher import FileWatcher

    ok = Path("/mem/ok.md")
    stuck = Path("/mem/stuck.md")

    class _Engine:
        async def index_file(self, path):
            if path == stuck:
                raise TimeoutError("sidecar held")

            class _Stats:
                indexed_chunks = 1
                skipped_chunks = 0
                deleted_chunks = 0

            return _Stats()

    watcher = FileWatcher(_Engine(), IndexingConfig(memory_dirs=[]))
    retry = await watcher._flush_batch({ok, stuck})
    assert retry == {stuck}


# ============================================================ E. sidecar hygiene


@pytest.mark.asyncio
async def test_sidecar_lockfiles_are_not_indexed(bm25_only_components):
    """A ``.{name}.md.lock`` sidecar living beside memory files is never picked
    up by a directory index — only the real markdown gets chunks."""
    comp, mem_dir = bm25_only_components
    (mem_dir / "real.md").write_text("## Real\n\nbody.\n", encoding="utf-8")
    # A sidecar lock as ``async_file_lock``/migrate leave behind.
    (mem_dir / ".real.md.lock").write_text("", encoding="utf-8")

    await comp.index_engine.index_path(mem_dir, recursive=True)

    sources = {p.name for p in await comp.storage.get_all_source_files()}
    assert "real.md" in sources
    assert not any(name.endswith(".lock") for name in sources), (
        f"a sidecar lockfile was indexed: {sorted(sources)}"
    )
