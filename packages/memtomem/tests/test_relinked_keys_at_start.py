"""At start, the watcher holds stored keys whose file became a symlink (#2904).

A relink made while no server or ``mm web`` was running produces no event, so
``FileWatcher`` checks every stored key once per start: a key whose leaf is a
symlink is held with ``source_relinked``. A parent directory that was moved and
linked back is not a relinked leaf.

Real ``SqliteBackend``, real symlinks, ``FileWatcher`` driven directly.
"""

from __future__ import annotations

import asyncio
import shutil
import sqlite3
import subprocess
import sys
import textwrap
import threading
import unicodedata
from pathlib import Path

import pytest

from memtomem.indexing import watcher as watcher_mod
from memtomem.indexing.watcher import FileWatcher
from memtomem.storage import orphan_detect, sqlite_helpers
from memtomem.storage.source_holds import RELINKED_HOLD
from memtomem.storage.sqlite_helpers import norm_path, stored_key_moved

BODY = "## Note\n\nstoredkeyword lives here\n"
TARGET = "## Other\n\ntargetkeyword lives here\n"

pytestmark = pytest.mark.requires_symlinks


@pytest.fixture
async def env(bm25_only_components):
    comp, mem_dir = bm25_only_components
    return comp, Path(norm_path(mem_dir))


async def _indexed(comp, path: Path, body: str = BODY) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    stats = await comp.index_engine.index_file(path)
    assert stats.indexed_chunks == 1, stats
    return path


def _relink(link: Path, target: Path) -> None:
    link.unlink()
    link.symlink_to(target)


def _reasons(comp) -> dict[str, str]:
    rows = comp.storage._get_db().execute("SELECT source_file, reason FROM held_sources")
    return {row[0]: row[1] for row in rows.fetchall()}


def _watcher(comp) -> FileWatcher:
    return FileWatcher(
        comp.index_engine, comp.config.indexing, search_pipeline=comp.search_pipeline
    )


def _spy_holds(monkeypatch, comp) -> list[Path]:
    calls: list[Path] = []
    real = comp.storage.hold_source

    async def spy(source_file, reason, **kwargs):
        if reason == RELINKED_HOLD:
            calls.append(Path(source_file))
        return await real(source_file, reason, **kwargs)

    monkeypatch.setattr(comp.storage, "hold_source", spy)
    return calls


@pytest.mark.parametrize("shape", ["indexed", "new", "dangling", "loop"])
async def test_a_file_relinked_while_no_watcher_ran_is_held(env, monkeypatch, shape):
    comp, root = env
    a = await _indexed(comp, root / "work" / "a.md")
    b = await _indexed(comp, root / "plans" / "b.md", TARGET)
    c = root / "plans" / "c.md"
    c.write_text(TARGET, encoding="utf-8")
    targets = {"indexed": b, "new": c, "dangling": root / "nowhere.md", "loop": a / "x"}
    _relink(a, targets[shape])
    invalidations: list[bool] = []
    monkeypatch.setattr(
        comp.search_pipeline, "invalidate_cache", lambda *a, **k: invalidations.append(True)
    )

    await _watcher(comp)._hold_relinked_keys()

    assert _reasons(comp) == {str(a): RELINKED_HOLD}
    assert invalidations


async def test_a_parent_moved_and_linked_back_is_not_held(env, monkeypatch):
    comp, root = env
    a = await _indexed(comp, root / "notes" / "a.md")
    (root / "notes").rename(root / "moved")
    (root / "notes").symlink_to(root / "moved")
    # The rule a scan over every moved key would have used says otherwise.
    assert stored_key_moved(str(a))
    holds = _spy_holds(monkeypatch, comp)

    await _watcher(comp)._hold_relinked_keys()

    assert holds == []
    assert _reasons(comp) == {}


async def test_a_file_indexed_through_a_link_is_not_held(env, monkeypatch):
    comp, root = env
    target = root / "plans" / "c.md"
    target.parent.mkdir(parents=True)
    target.write_text(TARGET, encoding="utf-8")
    link = root / "work" / "l.md"
    link.parent.mkdir(parents=True)
    link.symlink_to(target)
    assert (await comp.index_engine.index_file(link)).indexed_chunks == 1
    holds = _spy_holds(monkeypatch, comp)

    await _watcher(comp)._hold_relinked_keys()

    assert holds == []


async def test_a_legacy_unfolded_key_is_left_alone(env, monkeypatch):
    comp, root = env
    indexed = await _indexed(comp, root / "work" / "a.md")
    b = await _indexed(comp, root / "plans" / "b.md", TARGET)
    nfd = root / "work" / unicodedata.normalize("NFD", "café.md")
    db = comp.storage._get_db()
    db.execute("UPDATE chunks SET source_file=? WHERE source_file=?", (str(nfd), str(indexed)))
    db.commit()
    indexed.unlink()
    nfd.symlink_to(b)
    monkeypatch.setattr(sqlite_helpers, "FOLDS_UNICODE_FORMS", True)
    assert sqlite_helpers.fold_path_form(str(nfd)) != str(nfd)
    holds = _spy_holds(monkeypatch, comp)

    await _watcher(comp)._hold_relinked_keys()

    # Indexing would release a hold under the folded key, never this one.
    assert holds == []


async def test_a_failed_hold_does_not_stop_the_others(env, monkeypatch, caplog):
    comp, root = env
    first = await _indexed(comp, root / "work" / "a.md")
    second = await _indexed(comp, root / "work" / "c.md")
    b = await _indexed(comp, root / "plans" / "b.md", TARGET)
    _relink(first, b)
    _relink(second, b)
    real = comp.storage.hold_source

    async def flaky(source_file, reason, **kwargs):
        if Path(source_file) == first:
            raise OSError("disk I/O error")
        return await real(source_file, reason, **kwargs)

    monkeypatch.setattr(comp.storage, "hold_source", flaky)
    monkeypatch.setattr(watcher_mod, "_RELINK_HOLD_RETRY_DELAYS_S", (0.01, 0.01))

    await _watcher(comp)._hold_relinked_keys()

    assert _reasons(comp) == {str(second): RELINKED_HOLD}
    assert f"Gave up holding relinked source {first}" in caplog.text


async def test_start_runs_the_check_and_stop_cancels_it(env, monkeypatch):
    comp, root = env
    a = await _indexed(comp, root / "work" / "a.md")
    b = await _indexed(comp, root / "plans" / "b.md", TARGET)
    _relink(a, b)
    reached = asyncio.Event()
    real = comp.storage.hold_source

    async def parked(source_file, reason, **kwargs):
        if reason == RELINKED_HOLD:
            reached.set()
            await asyncio.Event().wait()
        return await real(source_file, reason, **kwargs)

    monkeypatch.setattr(comp.storage, "hold_source", parked)
    watcher = _watcher(comp)
    await watcher.start()
    try:
        await asyncio.wait_for(reached.wait(), timeout=5)
        task = watcher._relink_scan_task
        assert task is not None and not task.done()
    finally:
        await asyncio.wait_for(watcher.stop(), timeout=10)
    assert task.cancelled()
    assert watcher._relink_scan_task is None


async def test_a_held_relinked_key_is_released_once_the_file_is_back(env):
    comp, root = env
    a = await _indexed(comp, root / "work" / "a.md")
    b = await _indexed(comp, root / "plans" / "b.md", TARGET)
    _relink(a, b)
    await _watcher(comp)._hold_relinked_keys()
    assert _reasons(comp) == {str(a): RELINKED_HOLD}

    a.unlink()
    shutil.copyfile(b, a)
    await comp.index_engine.index_file(a)

    assert _reasons(comp) == {}


async def _until(predicate, timeout: float = 5.0) -> None:
    for _ in range(int(timeout / 0.01)):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition never held")


def _stall_after_first(monkeypatch, release: threading.Event) -> list[int]:
    """Make the first scan block in its thread after handing over its first key."""
    scans: list[int] = []
    real = orphan_detect.iter_relinked_leaf_keys

    def stalling(keys):
        scans.append(1)
        first_scan = len(scans) == 1
        for key in real(keys):
            yield key
            if first_scan:
                release.wait(10)

    monkeypatch.setattr(orphan_detect, "iter_relinked_leaf_keys", stalling)
    return scans


class _Store:
    """The two storage calls the start check makes, for a check run without real storage."""

    def __init__(self, keys: set[Path]) -> None:
        self.keys = keys

    async def get_orphan_candidate_source_files(self) -> set[Path]:
        return self.keys

    async def hold_source(self, source_file, reason, **kwargs) -> bool:
        return True


def _bare_watcher(tmp_path: Path, keys: set[Path]) -> FileWatcher:
    from types import SimpleNamespace

    from memtomem.config import IndexingConfig

    engine = SimpleNamespace(_storage=_Store(keys))
    return FileWatcher(engine, IndexingConfig(memory_dirs=[tmp_path]))


def test_a_check_outliving_its_event_loop_finishes_quietly(tmp_path, monkeypatch):
    """The thread hands keys to a loop that has closed; that is dropped, not raised."""
    key = tmp_path / "a.md"
    key.symlink_to(tmp_path / "nowhere.md")
    release = threading.Event()
    _stall_after_first(monkeypatch, release)
    raised: list[BaseException] = []
    monkeypatch.setattr(threading, "excepthook", lambda args: raised.append(args.exc_value))
    watcher = _bare_watcher(tmp_path, {key})
    loop = asyncio.new_event_loop()
    try:
        with pytest.raises(TimeoutError):
            loop.run_until_complete(asyncio.wait_for(watcher._hold_relinked_keys(), 0.2))
    finally:
        loop.close()
        release.set()
    watcher._relink_scan_thread.join(5)

    assert not watcher._relink_scan_thread.is_alive()
    assert raised == []


async def test_a_check_that_fails_in_its_thread_is_logged(tmp_path, monkeypatch, caplog):
    def failing(keys):
        raise OSError("mount went away")
        yield  # pragma: no cover

    monkeypatch.setattr(orphan_detect, "iter_relinked_leaf_keys", failing)
    raised: list[BaseException] = []
    monkeypatch.setattr(threading, "excepthook", lambda args: raised.append(args.exc_value))
    watcher = _bare_watcher(tmp_path, {tmp_path / "a.md"})

    await asyncio.wait_for(watcher._hold_relinked_keys(), timeout=5)

    assert raised == []
    assert "Relink check failed: mount went away" in caplog.text


async def test_a_thread_that_cannot_start_skips_the_check(tmp_path, monkeypatch, caplog):
    from types import SimpleNamespace

    class Unstartable(threading.Thread):
        def start(self) -> None:
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(watcher_mod, "threading", SimpleNamespace(Thread=Unstartable))
    watcher = _bare_watcher(tmp_path, {tmp_path / "a.md"})

    await asyncio.wait_for(watcher._hold_relinked_keys(), timeout=5)

    assert watcher._relink_scan_thread is None
    assert "Could not start the relink check" in caplog.text


def test_a_stalled_check_does_not_hold_up_process_exit(tmp_path):
    script = textwrap.dedent(
        """
        import asyncio
        import threading
        from pathlib import Path
        from types import SimpleNamespace

        from memtomem.config import IndexingConfig
        from memtomem.indexing.watcher import FileWatcher
        from memtomem.storage import orphan_detect

        class Store:
            async def get_orphan_candidate_source_files(self):
                return {Path(KEY)}

            async def hold_source(self, *args, **kwargs):
                return True

        def never(keys):
            threading.Event().wait()
            yield from ()

        orphan_detect.iter_relinked_leaf_keys = never

        async def main():
            engine = SimpleNamespace(_storage=Store())
            watcher = FileWatcher(engine, IndexingConfig(memory_dirs=[Path(KEY).parent]))
            task = asyncio.create_task(watcher._hold_relinked_keys())
            await asyncio.sleep(0.2)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        asyncio.run(main())
        print("exited")
        """
    ).replace("KEY", repr(str(tmp_path / "a.md")))

    proc = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "exited"


class _Abort(BaseException):
    pass


async def test_a_scan_killed_by_a_base_exception_still_ends_the_check(tmp_path, monkeypatch):
    def aborting(keys):
        raise _Abort
        yield  # pragma: no cover

    monkeypatch.setattr(orphan_detect, "iter_relinked_leaf_keys", aborting)
    raised: list[BaseException] = []
    monkeypatch.setattr(threading, "excepthook", lambda args: raised.append(args.exc_value))
    watcher = _bare_watcher(tmp_path, {tmp_path / "a.md"})

    await asyncio.wait_for(watcher._hold_relinked_keys(), timeout=5)

    await asyncio.to_thread(watcher._relink_scan_thread.join, 5)
    assert [type(exc) for exc in raised] == [_Abort]


async def test_a_restart_waits_for_a_stalled_check_then_checks_afresh(env, monkeypatch):
    """What the first thread finds after its task was stopped is not lost."""
    comp, root = env
    a = await _indexed(comp, root / "work" / "a.md")
    c = await _indexed(comp, root / "work" / "c.md")
    b = await _indexed(comp, root / "plans" / "b.md", TARGET)
    _relink(a, b)
    _relink(c, b)
    monkeypatch.setattr(watcher_mod, "_RELINK_WAIT_POLL_S", 0.02)
    release = threading.Event()
    scans = _stall_after_first(monkeypatch, release)
    watcher = _watcher(comp)
    try:
        await watcher.start()
        await _until(lambda: _reasons(comp) == {str(a): RELINKED_HOLD})
        await asyncio.wait_for(watcher.stop(), timeout=5)
        await watcher.start()
        await asyncio.sleep(0.1)
        # Still waiting on the first thread; no second scan yet.
        assert not watcher._relink_scan_task.done()
        assert len(scans) == 1
        release.set()
        await asyncio.wait_for(watcher._relink_scan_task, timeout=5)
    finally:
        release.set()
        await watcher.stop()

    assert len(scans) == 2
    assert _reasons(comp) == {str(a): RELINKED_HOLD, str(c): RELINKED_HOLD}


async def test_a_hold_that_fails_once_is_retried(env, monkeypatch):
    comp, root = env
    a = await _indexed(comp, root / "work" / "a.md")
    b = await _indexed(comp, root / "plans" / "b.md", TARGET)
    _relink(a, b)
    monkeypatch.setattr(watcher_mod, "_RELINK_HOLD_RETRY_DELAYS_S", (0.01, 0.01, 0.01))
    real = comp.storage.hold_source
    failures = [sqlite3.OperationalError("database is locked")]

    async def flaky(source_file, reason, **kwargs):
        if reason == RELINKED_HOLD and failures:
            raise failures.pop()
        return await real(source_file, reason, **kwargs)

    monkeypatch.setattr(comp.storage, "hold_source", flaky)

    await _watcher(comp)._hold_relinked_keys()

    assert failures == []
    assert _reasons(comp) == {str(a): RELINKED_HOLD}


async def test_a_hold_wakes_the_recheck_so_a_restored_file_is_released(env, monkeypatch):
    """The thread saw a link; the file was back and indexed before the hold landed."""
    comp, root = env
    a = await _indexed(comp, root / "work" / "a.md")
    b = await _indexed(comp, root / "plans" / "b.md", TARGET)
    _relink(a, b)
    real = orphan_detect.iter_relinked_leaf_keys

    def restored_before_hand_over(keys):
        for key in real(keys):
            key.unlink()
            key.write_text(BODY, encoding="utf-8")
            yield key

    monkeypatch.setattr(orphan_detect, "iter_relinked_leaf_keys", restored_before_hand_over)
    watcher = _watcher(comp)

    await watcher._hold_relinked_keys()

    assert _reasons(comp) == {str(a): RELINKED_HOLD}
    assert watcher._recheck_wakeup.is_set()
    recheck = asyncio.create_task(watcher._recheck_held_loop())
    try:
        await _until(lambda: _reasons(comp) == {})
    finally:
        recheck.cancel()
        await asyncio.gather(recheck, return_exceptions=True)
    assert (await comp.search_pipeline.search("storedkeyword"))[0]
