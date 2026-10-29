"""The watcher holds a source whose file became a symlink (#2878).

``index_file`` resolves its path and indexes the file a link names, so the rows
stored under the link's own key used to stay searchable while the path
presented another file. The watcher sees the leaf change (``_reindex``) and
owns the held recheck that feeds stored keys back into ``index_file``; both now
hold the stored key as stored, with ``source_relinked``, which only a clean
index pass of the path lifts.

Real ``SqliteBackend``, real symlinks, ``FileWatcher`` driven directly.
"""

from __future__ import annotations

import asyncio
import sqlite3
import unicodedata
from pathlib import Path
from typing import Literal

import pytest

from memtomem.indexing.watcher import FileWatcher
from memtomem.storage import orphan_detect, sqlite_helpers
from memtomem.storage.orphan_detect import hold_orphans, scan_orphans
from memtomem.storage.source_holds import REDACTION_BLOCKED_HOLD, RELINKED_HOLD
from memtomem.storage.sqlite_helpers import norm_path

BODY = "## Note\n\nstoredkeyword lives here\n"
TARGET = "## Other\n\ntargetkeyword lives here\n"
_SECRET = "AKIA" + "IOSFODNN7EXAMPLE"

pytestmark = pytest.mark.requires_symlinks


@pytest.fixture
async def env(bm25_only_components, monkeypatch):
    comp, mem_dir = bm25_only_components
    monkeypatch.setattr(orphan_detect, "ORPHAN_RECHECK_DELAY_SECONDS", 0.0)
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


async def _reason(comp, key: Path) -> str | None:
    row = (
        comp.storage._get_db()
        .execute("SELECT reason FROM held_sources WHERE source_file=?", (str(key),))
        .fetchone()
    )
    return row[0] if row else None


async def _pending(comp, key: Path) -> bool:
    return key in await comp.storage.get_pending_source_checks()


async def _found(comp, word: str) -> bool:
    comp.search_pipeline.invalidate_cache()
    return bool((await comp.search_pipeline.search(word))[0])


async def _recheck_until(watcher: FileWatcher, done) -> asyncio.Task:
    """Start the held recheck and return it, still running, once ``done()`` holds."""
    task = asyncio.create_task(watcher._recheck_held_loop())
    for _ in range(500):
        if await done():
            assert not task.done(), task
            return task
        await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    raise AssertionError("recheck loop never reached the expected state")


async def _stop(task: asyncio.Task) -> None:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def _count_index_calls(monkeypatch, engine) -> tuple[list[Path], list[Path]]:
    """``(started, finished)`` paths of every ``index_file`` call from here on."""
    started: list[Path] = []
    finished: list[Path] = []
    real = engine.index_file

    async def counting(path, *args, **kwargs):
        started.append(Path(path))
        try:
            return await real(path, *args, **kwargs)
        finally:
            finished.append(Path(path))

    monkeypatch.setattr(engine, "index_file", counting)
    return started, finished


class _ParkCounter(asyncio.Event):
    """The recheck's wakeup event, counting the loop's waits and their wakeups."""

    def __init__(self) -> None:
        super().__init__()
        self.parks = 0
        self.wakeups = 0

    async def wait(self) -> Literal[True]:
        self.parks += 1
        result = await super().wait()
        self.wakeups += 1
        return result


class TestLiveEvent:
    @pytest.mark.parametrize("journaled", [False, True], ids=["event", "journaled-delete"])
    async def test_a_link_to_a_new_file_holds_the_link_and_indexes_the_target(self, env, journaled):
        comp, root = env
        a = await _indexed(comp, root / "work" / "a.md")
        c = root / "plans" / "c.md"
        c.parent.mkdir(parents=True)
        c.write_text(TARGET, encoding="utf-8")
        assert await _found(comp, "storedkeyword")
        if journaled:
            assert comp.storage.queue_source_check_sync(a)
        _relink(a, c)

        watcher = FileWatcher(comp.index_engine, comp.config.indexing)
        assert await watcher._reindex(a) is None

        assert await _reason(comp, a) == RELINKED_HOLD
        assert not await _pending(comp, a)
        assert not await _found(comp, "storedkeyword")
        assert await _found(comp, "targetkeyword")

    async def test_a_link_to_an_indexed_file_leaves_that_file_alone(self, env):
        comp, root = env
        a = await _indexed(comp, root / "work" / "a.md")
        b = await _indexed(comp, root / "plans" / "b.md", TARGET)
        _relink(a, b)

        watcher = FileWatcher(comp.index_engine, comp.config.indexing)
        await watcher._reindex(a)

        assert await _reason(comp, a) == RELINKED_HOLD
        assert await _reason(comp, b) is None
        assert await _found(comp, "targetkeyword")

    async def test_a_failed_hold_is_retried(self, env, monkeypatch):
        comp, root = env
        a = await _indexed(comp, root / "work" / "a.md")
        b = await _indexed(comp, root / "plans" / "b.md", TARGET)
        _relink(a, b)
        real = comp.storage.hold_source
        failures = [sqlite3.OperationalError("database is locked")]

        async def flaky(source_file, reason, **kwargs):
            if reason == RELINKED_HOLD and failures:
                raise failures.pop()
            return await real(source_file, reason, **kwargs)

        monkeypatch.setattr(comp.storage, "hold_source", flaky)
        watcher = FileWatcher(comp.index_engine, comp.config.indexing)

        # Nothing journaled this path, so only the retry can still hide it.
        assert await watcher._reindex(a) == a
        assert await _reason(comp, a) is None
        assert not await _pending(comp, a)
        assert await watcher._reindex(a) is None
        assert await _reason(comp, a) == RELINKED_HOLD

    async def test_a_regular_file_is_not_held(self, env, monkeypatch):
        comp, root = env
        a = await _indexed(comp, root / "work" / "a.md")
        reasons: list[str] = []
        real = comp.storage.hold_source

        async def spy(source_file, reason, **kwargs):
            reasons.append(reason)
            return await real(source_file, reason, **kwargs)

        monkeypatch.setattr(comp.storage, "hold_source", spy)
        a.write_text(BODY + "\nmore\n", encoding="utf-8")

        await FileWatcher(comp.index_engine, comp.config.indexing)._reindex(a)

        assert RELINKED_HOLD not in reasons
        assert await _reason(comp, a) is None

    async def test_the_hold_lifts_when_the_path_is_a_regular_file_again(self, env):
        comp, root = env
        a = await _indexed(comp, root / "work" / "a.md")
        b = await _indexed(comp, root / "plans" / "b.md", TARGET)
        _relink(a, b)
        watcher = FileWatcher(comp.index_engine, comp.config.indexing)
        await watcher._reindex(a)
        assert await _reason(comp, a) == RELINKED_HOLD

        a.unlink()
        a.write_text("## Back\n\nreturnedkeyword lives here\n", encoding="utf-8")
        await watcher._reindex(a)

        assert await _reason(comp, a) is None
        assert await _found(comp, "returnedkeyword")
        assert not await _found(comp, "storedkeyword")


class TestHeldRecheck:
    async def test_a_pending_relinked_key_is_settled_and_does_not_spin(self, env, monkeypatch):
        comp, root = env
        a = await _indexed(comp, root / "work" / "a.md")
        b = await _indexed(comp, root / "plans" / "b.md", TARGET)
        assert comp.storage.queue_source_check_sync(a)
        _relink(a, b)
        _, finished = _count_index_calls(monkeypatch, comp.index_engine)
        watcher = FileWatcher(comp.index_engine, comp.config.indexing)
        watcher._recheck_wakeup = parked = _ParkCounter()

        async def settled() -> bool:
            # The first pass has run to its end and the loop is waiting.
            return (
                bool(finished)
                and parked.parks >= 1
                and await _reason(comp, a) == RELINKED_HOLD
                and not await _pending(comp, a)
            )

        task = await _recheck_until(watcher, settled)
        try:
            await asyncio.sleep(0.3)
            # Nothing set the wakeup, so the loop is still in its first wait.
            assert (parked.parks, parked.wakeups) == (1, 0)
            assert not task.done()
        finally:
            await _stop(task)
        assert await _reason(comp, b) is None
        assert not await _pending(comp, b)

    async def test_a_target_the_guard_refuses_does_not_leave_the_key_pending(self, env):
        comp, root = env
        a = await _indexed(comp, root / "work" / "a.md")
        b = await _indexed(comp, root / "plans" / "b.md", TARGET)
        b.write_text(TARGET + f"\naws key {_SECRET}\n", encoding="utf-8")
        assert comp.storage.queue_source_check_sync(a)
        _relink(a, b)

        watcher = FileWatcher(comp.index_engine, comp.config.indexing)

        async def settled() -> bool:
            return (
                await _reason(comp, a) == RELINKED_HOLD
                and not await _pending(comp, a)
                and await _reason(comp, b) == REDACTION_BLOCKED_HOLD
            )

        await _stop(await _recheck_until(watcher, settled))

    async def test_a_legacy_nfd_key_is_held_as_stored(self, env, monkeypatch):
        comp, root = env
        indexed = await _indexed(comp, root / "work" / "a.md")
        b = await _indexed(comp, root / "plans" / "b.md", TARGET)
        nfd = root / "work" / unicodedata.normalize("NFD", "café.md")
        db = comp.storage._get_db()
        db.execute("UPDATE chunks SET source_file=? WHERE source_file=?", (str(nfd), str(indexed)))
        db.commit()
        indexed.unlink()
        nfd.symlink_to(b)
        # Journal the NFD key as stored, then fold as macOS does: a key from
        # before keys were folded must still be held and settled as it is.
        monkeypatch.setattr(sqlite_helpers, "FOLDS_UNICODE_FORMS", False)
        assert comp.storage.queue_source_check_sync(nfd)
        monkeypatch.setattr(sqlite_helpers, "FOLDS_UNICODE_FORMS", True)
        assert sqlite_helpers.fold_path_form(str(nfd)) != str(nfd)
        assert await _pending(comp, nfd)

        watcher = FileWatcher(comp.index_engine, comp.config.indexing)

        async def settled() -> bool:
            return await _reason(comp, nfd) == RELINKED_HOLD and not await _pending(comp, nfd)

        await _stop(await _recheck_until(watcher, settled))

    async def test_outside_the_roots_presence_does_not_lift_a_relinked_hold(self, env, monkeypatch):
        comp, root = env
        a = await _indexed(comp, root / "old" / "a.md")
        b = await _indexed(comp, root / "elsewhere" / "b.md", TARGET)
        _relink(a, b)
        assert await comp.storage.hold_source(a, RELINKED_HOLD, as_stored=True)
        a.unlink()
        a.write_text("## Replaced\n\nreplacedkeyword\n", encoding="utf-8")
        (root / "plans").mkdir()
        comp.config.indexing.memory_dirs = [root / "plans"]

        released: list[tuple[Path, bool]] = []
        real = comp.storage.release_source_hold

        async def spy(source_file, **kwargs):
            result = await real(source_file, **kwargs)
            if kwargs.get("validated") is False:
                released.append((Path(source_file), result))
            return result

        monkeypatch.setattr(comp.storage, "release_source_hold", spy)
        watcher = FileWatcher(comp.index_engine, comp.config.indexing)

        async def tried() -> bool:
            return bool(released)

        await _stop(await _recheck_until(watcher, tried))
        assert released == [(a, False)]
        assert await _reason(comp, a) == RELINKED_HOLD
        assert not await _found(comp, "storedkeyword")


async def test_storage_keeps_a_relinked_hold_on_presence_only_release(env):
    comp, root = env
    a = await _indexed(comp, root / "work" / "a.md")
    assert await comp.storage.hold_source(a, RELINKED_HOLD, as_stored=True)

    assert not await comp.storage.release_source_hold(a, validated=False, as_stored=True)
    assert await _reason(comp, a) == RELINKED_HOLD
    assert await comp.storage.release_source_hold(a, validated=True, as_stored=True)
    assert await _reason(comp, a) is None


async def test_retained_pending_key_whose_release_is_refused_is_held_as_stored(env):
    """#2878 section 3: the retained-and-pending branch settles the stored key itself.

    The key keeps a non-absence hold, so ``release_absence_hold`` refuses it, and
    it is a dangling link, so resolving it again would name the target instead.
    """
    comp, root = env
    a = await _indexed(comp, root / "work" / "a.md")
    target = root / "nowhere.md"
    comp.config.indexing.retain_missing_dirs = [root]
    assert await comp.storage.hold_source(a, "scan_unavailable", as_stored=True)
    assert comp.storage.queue_source_check_sync(a)
    _relink(a, target)
    assert norm_path(a) == norm_path(target) != str(a)
    assert await _pending(comp, a)

    watcher = FileWatcher(comp.index_engine, comp.config.indexing)

    async def settled() -> bool:
        return not await _pending(comp, a)

    await _stop(await _recheck_until(watcher, settled))
    assert await _reason(comp, a) == "watch_reindex_unresolved"
    assert await _reason(comp, Path(norm_path(target))) is None
    assert not await _pending(comp, Path(norm_path(target)))


class TestReasonPrecedence:
    """``source_relinked`` keeps its reason against availability observations."""

    async def test_an_availability_reason_does_not_replace_a_relink(self, env):
        comp, root = env
        a = await _indexed(comp, root / "work" / "a.md")
        assert await comp.storage.hold_source(a, RELINKED_HOLD, as_stored=True)

        for reason in ("scan_unavailable", "scan_missing", "watch_reindex_unresolved"):
            assert not await comp.storage.hold_source(a, reason, as_stored=True)
            assert await _reason(comp, a) == RELINKED_HOLD

        assert not await comp.storage.release_source_hold(a, validated=False, as_stored=True)
        assert await _reason(comp, a) == RELINKED_HOLD

    async def test_settling_a_pending_check_keeps_the_relink(self, env):
        # The watcher's ``finally`` settle: a journaled delete on a relinked key
        # reaches the write, which clears the pending row but keeps the reason.
        comp, root = env
        a = await _indexed(comp, root / "work" / "a.md")
        assert await comp.storage.hold_source(a, RELINKED_HOLD, as_stored=True)
        assert comp.storage.queue_source_check_sync(a)

        assert await comp.storage.hold_source(a, "watch_reindex_unresolved", as_stored=True)

        assert not await _pending(comp, a)
        assert await _reason(comp, a) == RELINKED_HOLD

    async def test_redaction_replaces_a_relink_and_not_the_reverse(self, env):
        comp, root = env
        a = await _indexed(comp, root / "work" / "a.md")
        assert await comp.storage.hold_source(a, RELINKED_HOLD, as_stored=True)
        before = await comp.storage.source_hold_generation(a)

        assert await comp.storage.hold_source(a, REDACTION_BLOCKED_HOLD, as_stored=True)
        assert await _reason(comp, a) == REDACTION_BLOCKED_HOLD
        assert await comp.storage.source_hold_generation(a) > before
        assert not await comp.storage.hold_source(a, RELINKED_HOLD, as_stored=True)
        assert await _reason(comp, a) == REDACTION_BLOCKED_HOLD

    async def test_raising_a_hold_to_relinked_voids_an_earlier_release(self, env):
        comp, root = env
        a = await _indexed(comp, root / "work" / "a.md")
        assert await comp.storage.hold_source(a, "scan_unavailable", as_stored=True)
        snapshot = await comp.storage.source_hold_snapshot(a)
        assert snapshot is not None

        assert await comp.storage.hold_source(a, RELINKED_HOLD, as_stored=True)

        assert not await comp.storage.release_source_hold(
            a, expected_generation=snapshot, as_stored=True
        )
        assert await _reason(comp, a) == RELINKED_HOLD

    async def test_a_retained_relinked_key_whose_target_vanished_stays_held(self, env):
        comp, root = env
        a = await _indexed(comp, root / "work" / "a.md")
        b = await _indexed(comp, root / "plans" / "b.md", TARGET)
        _relink(a, b)
        assert await comp.storage.hold_source(a, RELINKED_HOLD, as_stored=True)
        b.unlink()
        comp.config.indexing.retain_missing_dirs = [root]

        result = await scan_orphans(comp.storage)
        assert a in result.confirmed_orphans
        await hold_orphans(comp.storage, result, comp.config.indexing)

        assert await _reason(comp, a) == RELINKED_HOLD
        assert not await _found(comp, "storedkeyword")
