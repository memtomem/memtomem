"""At start, the watcher journals stored keys whose file became a symlink (#2904).

A relink made while no server or ``mm web`` was running produces no event, so
``FileWatcher`` checks every stored key once per start: a key whose leaf is a
symlink is journaled as a pending source check, which hides it at once, and the
held recheck settles it (#2905). A parent directory that was moved and linked
back is not a relinked leaf.

Real ``SqliteBackend``, real symlinks, ``FileWatcher`` driven directly.
"""

from __future__ import annotations

import asyncio
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


def _pending(comp) -> set[str]:
    rows = comp.storage._get_db().execute("SELECT source_file FROM pending_source_checks")
    return {row[0] for row in rows.fetchall()}


async def _found(comp, word: str) -> bool:
    return bool((await comp.search_pipeline.search(word))[0])


def _watcher(comp) -> FileWatcher:
    return FileWatcher(
        comp.index_engine, comp.config.indexing, search_pipeline=comp.search_pipeline
    )


async def _check(watcher: FileWatcher) -> None:
    """Run the start check and wait for its thread to finish."""
    await watcher._journal_relinked_keys()
    if watcher._relink_scan_thread is not None:
        await asyncio.to_thread(watcher._relink_scan_thread.join, 10)
        assert not watcher._relink_scan_thread.is_alive()
    await asyncio.sleep(0)


async def _until(predicate, timeout: float = 5.0) -> None:
    for _ in range(int(timeout / 0.01)):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition never held")


async def _settle(watcher: FileWatcher, predicate) -> None:
    """Run the held recheck until ``predicate()`` holds."""
    task = asyncio.create_task(watcher._recheck_held_loop())
    try:
        await _until(predicate)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize(
    ("shape", "settled_reason"),
    [
        ("indexed", RELINKED_HOLD),
        ("new", RELINKED_HOLD),
        ("dangling", "watch_reindex_unresolved"),
        ("loop", "watch_reindex_unresolved"),
    ],
)
async def test_a_file_relinked_while_no_watcher_ran_is_hidden_and_settled(
    env, shape, settled_reason
):
    comp, root = env
    a = await _indexed(comp, root / "work" / "a.md")
    b = await _indexed(comp, root / "plans" / "b.md", TARGET)
    c = root / "plans" / "c.md"
    c.write_text(TARGET, encoding="utf-8")
    targets = {"indexed": b, "new": c, "dangling": root / "nowhere.md", "loop": a / "x"}
    _relink(a, targets[shape])
    assert await _found(comp, "storedkeyword")
    watcher = _watcher(comp)

    await _check(watcher)

    assert _pending(comp) == {str(a)}
    assert not await _found(comp, "storedkeyword")
    assert watcher._recheck_wakeup.is_set()
    await _settle(watcher, lambda: str(a) not in _pending(comp))
    assert _reasons(comp).get(str(a)) == settled_reason
    assert str(b) not in _reasons(comp)
    assert not await _found(comp, "storedkeyword")


async def test_a_parent_moved_and_linked_back_is_not_journaled(env):
    comp, root = env
    a = await _indexed(comp, root / "notes" / "a.md")
    (root / "notes").rename(root / "moved")
    (root / "notes").symlink_to(root / "moved")
    # The rule a check over every moved key would have used says otherwise.
    assert stored_key_moved(str(a))

    await _check(_watcher(comp))

    assert _pending(comp) == set()


async def test_a_file_indexed_through_a_link_is_not_journaled(env):
    comp, root = env
    target = root / "plans" / "c.md"
    target.parent.mkdir(parents=True)
    target.write_text(TARGET, encoding="utf-8")
    link = root / "work" / "l.md"
    link.parent.mkdir(parents=True)
    link.symlink_to(target)
    assert (await comp.index_engine.index_file(link)).indexed_chunks == 1

    await _check(_watcher(comp))

    assert _pending(comp) == set()


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
    journaled: list[Path] = []
    monkeypatch.setattr(
        type(comp.storage),
        "queue_source_check_sync",
        lambda self, key: journaled.append(Path(key)) or True,
    )

    await _check(_watcher(comp))

    # Indexing would release a hold under the folded key, never this one (#2907).
    assert journaled == []


async def test_a_file_restored_before_the_recheck_is_shown_again(env, monkeypatch):
    """The thread saw a link; the file was back before the recheck looked."""
    comp, root = env
    a = await _indexed(comp, root / "work" / "a.md")
    b = await _indexed(comp, root / "plans" / "b.md", TARGET)
    _relink(a, b)
    real = orphan_detect.iter_relinked_leaf_keys

    def restored_after_the_probe(keys):
        for key in real(keys):
            key.unlink()
            key.write_text(BODY, encoding="utf-8")
            yield key

    monkeypatch.setattr(orphan_detect, "iter_relinked_leaf_keys", restored_after_the_probe)
    watcher = _watcher(comp)

    await _check(watcher)

    assert _pending(comp) == {str(a)}
    await _settle(watcher, lambda: not _pending(comp))
    assert _reasons(comp) == {}
    assert await _found(comp, "storedkeyword")


async def test_a_journal_write_that_meets_a_busy_database_is_retried(env, monkeypatch):
    comp, root = env
    a = await _indexed(comp, root / "work" / "a.md")
    b = await _indexed(comp, root / "plans" / "b.md", TARGET)
    _relink(a, b)
    monkeypatch.setattr(watcher_mod, "_RELINK_JOURNAL_RETRY_DELAYS_S", (0.01, 0.01))
    real = type(comp.storage).queue_source_check_sync
    failures = [sqlite3.OperationalError("database is locked")]

    def flaky(self, key):
        if failures:
            raise failures.pop()
        return real(self, key)

    monkeypatch.setattr(type(comp.storage), "queue_source_check_sync", flaky)

    await _check(_watcher(comp))

    assert failures == []
    assert _pending(comp) == {str(a)}


async def test_a_journal_write_that_keeps_failing_is_logged(env, monkeypatch, caplog):
    comp, root = env
    first = await _indexed(comp, root / "work" / "a.md")
    second = await _indexed(comp, root / "work" / "c.md")
    b = await _indexed(comp, root / "plans" / "b.md", TARGET)
    _relink(first, b)
    _relink(second, b)
    monkeypatch.setattr(watcher_mod, "_RELINK_JOURNAL_RETRY_DELAYS_S", (0.01, 0.01))
    real = type(comp.storage).queue_source_check_sync

    def flaky(self, key):
        if Path(key) == first:
            raise sqlite3.OperationalError("database is locked")
        return real(self, key)

    monkeypatch.setattr(type(comp.storage), "queue_source_check_sync", flaky)

    await _check(_watcher(comp))

    assert _pending(comp) == {str(second)}
    assert f"Could not journal relinked source {first}" in caplog.text


def _stall_after_first(monkeypatch, release: threading.Event) -> list[int]:
    """Make the first scan block in its thread after its first relinked key."""
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


async def test_a_restart_waits_for_a_stalled_check_then_checks_afresh(env, monkeypatch):
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
        # The first key is journaled, and so hidden, while the thread is stalled.
        await _until(lambda: str(a) in _pending(comp) or str(a) in _reasons(comp))
        await asyncio.wait_for(watcher.stop(), timeout=5)
        await watcher.start()
        await asyncio.sleep(0.1)
        assert not watcher._relink_scan_task.done()
        assert len(scans) == 1
        release.set()
        await asyncio.wait_for(watcher._relink_scan_task, timeout=5)
        await asyncio.to_thread(watcher._relink_scan_thread.join, 5)
    finally:
        release.set()
        await watcher.stop()

    assert len(scans) == 2
    hidden = _pending(comp) | set(_reasons(comp))
    assert {str(a), str(c)} <= hidden


async def test_start_runs_the_check_and_stop_cancels_it(env, monkeypatch):
    comp, root = env
    reached = asyncio.Event()

    async def parked(_storage):
        reached.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(orphan_detect, "orphan_candidate_sources", parked)
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


class _Store:
    """The two storage calls the start check makes, without real storage."""

    def __init__(self, keys: set[Path]) -> None:
        self.keys = keys
        self.journaled: list[Path] = []

    async def get_orphan_candidate_source_files(self) -> set[Path]:
        return self.keys

    def queue_source_check_sync(self, key: Path) -> bool:
        self.journaled.append(key)
        return True


def _bare_watcher(tmp_path: Path, keys: set[Path]) -> FileWatcher:
    from types import SimpleNamespace

    from memtomem.config import IndexingConfig

    engine = SimpleNamespace(_storage=_Store(keys))
    return FileWatcher(engine, IndexingConfig(memory_dirs=[tmp_path]))


def test_a_check_outliving_its_event_loop_finishes_quietly(tmp_path, monkeypatch):
    """The thread wakes a loop that has closed; that is dropped, not raised."""
    key = tmp_path / "a.md"
    key.symlink_to(tmp_path / "nowhere.md")
    release = threading.Event()
    real = orphan_detect.iter_relinked_leaf_keys

    def late(keys):
        release.wait(10)
        yield from real(keys)

    monkeypatch.setattr(orphan_detect, "iter_relinked_leaf_keys", late)
    raised: list[BaseException] = []
    monkeypatch.setattr(threading, "excepthook", lambda args: raised.append(args.exc_value))
    watcher = _bare_watcher(tmp_path, {key})
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(watcher._journal_relinked_keys())
    finally:
        loop.close()
        release.set()
    watcher._relink_scan_thread.join(5)

    assert not watcher._relink_scan_thread.is_alive()
    assert watcher._engine._storage.journaled == [key]
    assert raised == []


async def test_a_check_that_fails_in_its_thread_is_logged(tmp_path, monkeypatch, caplog):
    def failing(keys):
        raise OSError("mount went away")
        yield  # pragma: no cover

    monkeypatch.setattr(orphan_detect, "iter_relinked_leaf_keys", failing)
    raised: list[BaseException] = []
    monkeypatch.setattr(threading, "excepthook", lambda args: raised.append(args.exc_value))
    watcher = _bare_watcher(tmp_path, {tmp_path / "a.md"})

    await _check(watcher)

    assert raised == []
    assert "Relink check failed: mount went away" in caplog.text


async def test_a_thread_that_cannot_start_skips_the_check(tmp_path, monkeypatch, caplog):
    from types import SimpleNamespace

    class Unstartable(threading.Thread):
        def start(self) -> None:
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(watcher_mod, "threading", SimpleNamespace(Thread=Unstartable))
    watcher = _bare_watcher(tmp_path, {tmp_path / "a.md"})

    await watcher._journal_relinked_keys()

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

            def queue_source_check_sync(self, key):
                return True

        def never(keys):
            threading.Event().wait()
            yield from ()

        orphan_detect.iter_relinked_leaf_keys = never

        async def main():
            engine = SimpleNamespace(_storage=Store())
            watcher = FileWatcher(engine, IndexingConfig(memory_dirs=[Path(KEY).parent]))
            await watcher._journal_relinked_keys()
            await asyncio.sleep(0.2)

        asyncio.run(main())
        print("exited")
        """
    ).replace("KEY", repr(str(tmp_path / "a.md")))

    proc = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "exited"


async def test_a_relinked_key_outside_every_root_is_shown_as_before(env):
    """The recheck shows a present source outside the roots, as since #2835."""
    comp, root = env
    a = await _indexed(comp, root / "old" / "a.md")
    b = await _indexed(comp, root / "elsewhere" / "b.md", TARGET)
    _relink(a, b)
    (root / "plans").mkdir()
    comp.config.indexing.memory_dirs = [root / "plans"]
    watcher = _watcher(comp)

    await _check(watcher)

    assert str(a) in _pending(comp)
    await _settle(watcher, lambda: str(a) not in _pending(comp))
    assert _reasons(comp) == {}


async def test_a_dangling_link_under_a_retain_dir_stays_hidden(env):
    """A retain dir shows the archive of a file that is gone, not of one replaced by a link."""
    comp, root = env
    a = await _indexed(comp, root / "work" / "a.md")
    _relink(a, root / "nowhere.md")
    comp.config.indexing.retain_missing_dirs = [root]
    watcher = _watcher(comp)

    await _check(watcher)

    assert _pending(comp) == {str(a)}
    await _settle(watcher, lambda: str(a) not in _pending(comp))
    assert _reasons(comp) == {str(a): "watch_reindex_unresolved"}
    assert not await _found(comp, "storedkeyword")


async def test_a_file_gone_from_a_retain_dir_is_still_shown_as_its_archive(env):
    """The control: nothing at the path, so the retain dir keeps showing it (#2833)."""
    comp, root = env
    a = await _indexed(comp, root / "work" / "a.md")
    a.unlink()
    comp.config.indexing.retain_missing_dirs = [root]
    assert comp.storage.queue_source_check_sync(a)
    watcher = _watcher(comp)

    await _settle(watcher, lambda: str(a) not in _pending(comp))

    assert _reasons(comp) == {}
    assert await _found(comp, "storedkeyword")
