"""The watcher drops a queued path whose root is no longer configured (#2528).

``FileWatcher`` collects changed paths and flushes them after a debounce, and
``reconfigure`` leaves the collected paths alone. Removing a root with
``delete_chunks`` sweeps its sources first, so a file edited under that root
just before the remove would be indexed again by the next flush — as an orphan
the user had asked to delete. The acceptance test drives the real web route
with a real store; the unit tests pin which paths the guard must still let
through, so a narrower containment rule fails here rather than dropping events.
"""

from __future__ import annotations

import asyncio
import os
import unicodedata
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from helpers import isolate_config_paths
from memtomem.config import IndexingConfig
from memtomem.indexing.watcher import _STOP_SENTINEL, FileWatcher
from memtomem.web import hot_reload
from memtomem.web.app import create_app
from memtomem.web.deps import require_configured


async def _chunk_contents(comp, source: Path) -> list[str]:
    return [c.content for c in await comp.storage.list_chunks_by_source(source)]


async def _indexed_sources(comp) -> set[str]:
    return {str(row[0]) for row in await comp.storage.get_source_files_with_counts()}


def _web_app(comp):
    """The web app wired to the real ``comp`` stack, without a lifespan."""
    app = create_app(lifespan=None, mode="dev")
    for name in ("storage", "index_engine", "search_pipeline", "config"):
        setattr(app.state, name, getattr(comp, name))
    app.dependency_overrides[require_configured] = lambda: None
    # Without a current signature ``reload_if_stale`` rebuilds the config from
    # disk and the route would act on that, not on the injected components.
    app.state.config_signature = hot_reload.current_signature()
    app.state.last_reload_error = None
    return app


async def test_queued_event_under_removed_root_is_not_reindexed(
    bm25_only_components, tmp_path, tmp_path_factory, monkeypatch
):
    comp, kept_root = bm25_only_components
    isolate_config_paths(monkeypatch, tmp_path_factory.mktemp("config-home"))
    removed_root = tmp_path / "removed"
    removed_root.mkdir()
    comp.config.indexing.memory_dirs = [kept_root, removed_root]

    kept = (kept_root / "kept.md").resolve()
    gone = (removed_root / "gone.md").resolve()
    kept.write_text("## Kept\n\nkept before\n", encoding="utf-8")
    gone.write_text("## Gone\n\ngone before\n", encoding="utf-8")
    for source in (kept, gone):
        stats = await comp.index_engine.index_file(source)
        assert stats.indexed_chunks == 1, source

    app = _web_app(comp)
    # Not started: the web app shares one ``IndexingConfig`` between engine and
    # watcher, and ``reconfigure`` on a stopped watcher only swaps that config.
    watcher = FileWatcher(comp.index_engine, comp.config.indexing)
    app.state.file_watcher = watcher

    # Both files change inside one debounce window, before the remove.
    kept.write_text("## Kept\n\nkept after\n", encoding="utf-8")
    gone.write_text("## Gone\n\ngone after\n", encoding="utf-8")
    watcher._queue.put_nowait(kept)
    watcher._queue.put_nowait(gone)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/memory-dirs/remove",
            json={"path": str(removed_root), "delete_chunks": True},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["deleted_chunks"] == 1
    # The sweep ran: whatever the flush does next is the watcher's doing.
    assert await _chunk_contents(comp, gone) == []
    assert watcher._config.all_index_roots() == [kept_root]

    watcher._queue.put_nowait(_STOP_SENTINEL)
    await watcher._process_events()

    assert await _chunk_contents(comp, gone) == []
    assert str(gone) not in await _indexed_sources(comp)
    # Control: the same flush still indexes the surviving root's edit, so the
    # guard is not simply skipping everything.
    (kept_body,) = await _chunk_contents(comp, kept)
    assert "kept after" in kept_body


async def test_indexing_in_flight_at_remove_writes_after_the_sweep(
    bm25_only_components, tmp_path, tmp_path_factory, monkeypatch
):
    """Known limit (#2534): the sweep does not wait for indexing already under
    way. An ``index_file`` that is waiting on the file's sidecar when the root
    is removed writes the file's chunks after the sweep deleted them. Taking
    ``_index_lock`` would not close this: the bulk path holds only sidecars."""
    from memtomem.context import _atomic

    comp, kept_root = bm25_only_components
    isolate_config_paths(monkeypatch, tmp_path_factory.mktemp("config-home"))
    removed_root = tmp_path / "removed"
    removed_root.mkdir()
    comp.config.indexing.memory_dirs = [kept_root, removed_root]
    gone = (removed_root / "gone.md").resolve()
    gone.write_text("## Gone\n\ngone before\n", encoding="utf-8")
    assert (await comp.index_engine.index_file(gone)).indexed_chunks == 1
    gone.write_text("## Gone\n\ngone after\n", encoding="utf-8")

    # The indexer waits on the sidecar for as long as the route may take (60 s),
    # so a slow sweep cannot time it out before the test releases the lock.
    monkeypatch.setattr(_atomic, "_MEMORY_SIDECAR_LOCK_BUDGET_S", 120.0)
    lock_path = _atomic.memory_lock_path(gone)
    task: asyncio.Task | None = None
    try:
        async with _atomic.async_file_lock(lock_path, timeout=5):
            # Installed after the test holds the sidecar, so only the indexer's
            # acquire can trip it.
            real_lock = _atomic.async_file_lock
            waiting = asyncio.Event()

            def _spy(path, *, timeout):
                if path == lock_path:
                    waiting.set()
                return real_lock(path, timeout=timeout)

            monkeypatch.setattr(_atomic, "async_file_lock", _spy)
            task = asyncio.create_task(comp.index_engine.index_file(gone))
            await asyncio.wait_for(waiting.wait(), timeout=5)
            for _ in range(5):
                await asyncio.sleep(0)
            assert not task.done()

            async with AsyncClient(
                transport=ASGITransport(app=_web_app(comp)), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/api/memory-dirs/remove",
                    json={"path": str(removed_root), "delete_chunks": True},
                )
            assert resp.status_code == 200, resp.text
            assert resp.json()["deleted_chunks"] == 1
            assert await _chunk_contents(comp, gone) == []
            assert not task.done()

        await asyncio.wait_for(task, timeout=10)
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    (body,) = await _chunk_contents(comp, gone)
    assert "gone after" in body


class _RecordingEngine:
    def __init__(self) -> None:
        self.calls: list[Path] = []

    async def index_file(self, path):
        self.calls.append(path)

        class _Stats:
            indexed_chunks = 1
            skipped_chunks = 0
            deleted_chunks = 0

        return _Stats()


@pytest.mark.parametrize("tier", ["memory_dirs", "project_memory_dirs", "read_only_memory_dirs"])
async def test_every_root_tier_is_still_reindexed(tmp_path, tier):
    root = tmp_path / "root"
    root.mkdir()
    note = root / "note.md"
    note.write_text("x", encoding="utf-8")
    kwargs = {"memory_dirs": []}
    kwargs[tier] = [root]
    engine = _RecordingEngine()
    watcher = FileWatcher(engine, IndexingConfig(**kwargs))

    assert await watcher._reindex(note) is None
    assert engine.calls == [note]


async def test_path_outside_every_root_is_dropped(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    engine = _RecordingEngine()
    watcher = FileWatcher(engine, IndexingConfig(memory_dirs=[root]))

    # A sibling sharing the root's name as a prefix is not inside it.
    assert await watcher._reindex(tmp_path / "root-other" / "note.md") is None
    assert engine.calls == []


async def test_nfd_spelled_path_under_nfc_root_is_reindexed(tmp_path):
    # macOS hands back decomposed names; users type composed ones. The remove
    # sweep compares NFC forms, so the guard must too, or it drops live events.
    name = "메모"
    root_nfc = tmp_path / unicodedata.normalize("NFC", name)
    note_nfd = tmp_path / unicodedata.normalize("NFD", name) / "note.md"
    assert str(root_nfc) != str(note_nfd.parent)
    engine = _RecordingEngine()
    watcher = FileWatcher(engine, IndexingConfig(memory_dirs=[root_nfc]))

    await watcher._reindex(note_nfd)
    assert engine.calls == [note_nfd]


async def test_symlink_in_root_pointing_outside_is_reindexed(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "target.md"
    target.write_text("x", encoding="utf-8")
    link = root / "link.md"
    try:
        os.symlink(target, link)
    except OSError as exc:  # Windows without the symlink privilege
        pytest.skip(f"cannot create a symlink here: {exc}")
    engine = _RecordingEngine()
    watcher = FileWatcher(engine, IndexingConfig(memory_dirs=[root]))

    # The root's own walk indexes this entry, so the watcher must as well.
    await watcher._reindex(link)
    assert engine.calls == [link]
