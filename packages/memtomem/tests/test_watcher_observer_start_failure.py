"""A watcher whose observer cannot start keeps the server up (#3080).

watchdog 7.0 re-raises a native stream that failed to start out of
``Observer.start`` (6.0 logged it in the emitter thread). Unguarded, that
raise failed ``FileWatcher.start``, and with it the MCP server's
initialization and ``mm web``'s lifespan, so every tool call failed. The
watcher now runs on without an observer: its source checks and the opt-in
backfill still run, ``observer_error`` says why, ``mem_status`` warns and the
``mm web`` embedding reset does not claim edits are auto-indexed.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from watchdog.observers.api import BaseObserver, EventEmitter
from watchdog.observers.polling import PollingObserver

from memtomem.indexing import watcher as watcher_mod
from memtomem.indexing.watcher import FileWatcher
from memtomem.server.component_factory import TeardownResult
from memtomem.server.context import AppContext
from memtomem.server.tools.status_config import collect_status_report
from memtomem.storage.sqlite_helpers import norm_path

FSEVENTS_MESSAGE = "Cannot start fsevents stream. Use a kqueue or polling observer instead."
FSEVENTS_ERROR = f"SystemError: {FSEVENTS_MESSAGE}"


class _SecondStartFails(EventEmitter):
    """An emitter set whose second ``start`` raises like watchdog 7.0's FSEvents.

    Failing the *second* start, whichever path it is, makes the partial start
    deterministic: watchdog iterates its emitters as a set, so failing a fixed
    path could fail first and leave nothing started to prove the cleanup on.
    """

    started: list[EventEmitter] = []

    def start(self) -> None:
        if len(type(self).started) == 1:
            raise SystemError(FSEVENTS_MESSAGE)
        super().start()
        type(self).started.append(self)

    def queue_events(self, timeout: float) -> None:
        time.sleep(timeout)


class _RaisingObserver:
    """An observer stand-in: ``start`` raises *exc*; ``stop`` raises while ``stop_fails``."""

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc
        self.stop_fails = False
        self.stop_calls = 0

    def schedule(self, handler, path, recursive=True):
        return SimpleNamespace(path=path)

    def start(self) -> None:
        raise self.exc

    def stop(self) -> None:
        self.stop_calls += 1
        if self.stop_fails:
            raise RuntimeError("emitter would not stop")

    def is_alive(self) -> bool:
        return False


@pytest.fixture
async def env(bm25_only_components, tmp_path):
    comp, mem_dir = bm25_only_components
    second = tmp_path / "second"
    second.mkdir()
    comp.config.indexing.memory_dirs = [mem_dir, second]
    return comp, Path(norm_path(mem_dir)), Path(norm_path(second))


def _watcher(comp) -> FileWatcher:
    return FileWatcher(
        comp.index_engine, comp.config.indexing, search_pipeline=comp.search_pipeline
    )


async def _found(comp, word: str) -> bool:
    return bool((await comp.search_pipeline.search(word))[0])


async def _until_async(predicate, timeout: float = 10.0) -> None:
    for _ in range(int(timeout / 0.02)):
        if await predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition never held")


def _background_tasks(watcher: FileWatcher) -> list:
    return [
        watcher._task,
        watcher._pending_replay_task,
        watcher._held_recheck_task,
        watcher._relink_scan_task,
    ]


async def test_a_partly_started_observer_is_stopped_and_the_watcher_runs_on(env):
    comp, first, second = env
    comp.config.indexing.startup_backfill = True
    (first / "a.md").write_text("## A\n\nfirstrootword\n", encoding="utf-8")
    (second / "b.md").write_text("## B\n\nsecondrootword\n", encoding="utf-8")
    _SecondStartFails.started = []
    watcher = _watcher(comp)

    with patch.object(
        watcher_mod, "_create_observer", lambda _c: BaseObserver(_SecondStartFails, timeout=0.05)
    ):
        await watcher.start()
    try:
        assert watcher.observer_error == FSEVENTS_ERROR
        assert watcher.live_events is False
        assert watcher._observer is None
        assert watcher._watches == {}
        assert watcher._failed_observer is None
        # One emitter really ran before the other failed, and it is gone now.
        assert len(_SecondStartFails.started) == 1
        assert not _SecondStartFails.started[0].is_alive()
        tasks = _background_tasks(watcher)
        assert all(task is not None and not task.done() for task in tasks)
        # The backfill walks every existing root, not only those scheduled
        # before the failure.
        await _until_async(lambda: _found(comp, "firstrootword"))
        await _until_async(lambda: _found(comp, "secondrootword"))
    finally:
        await watcher.stop()
    assert watcher.live_events is False


@pytest.mark.parametrize("fail_at", ["construct", "second_schedule"])
async def test_backfill_covers_every_root_when_the_observer_fails_before_starting(env, fail_at):
    comp, first, second = env
    comp.config.indexing.startup_backfill = True
    (first / "a.md").write_text("## A\n\nfirstrootword\n", encoding="utf-8")
    (second / "b.md").write_text("## B\n\nsecondrootword\n", encoding="utf-8")
    observer = _RaisingObserver(SystemError(FSEVENTS_MESSAGE))
    scheduled: list[str] = []

    def schedule(handler, path, recursive=True):
        if scheduled:
            raise OSError(28, "inotify watch limit reached")
        scheduled.append(path)
        return SimpleNamespace(path=path)

    observer.schedule = schedule

    def create(_config):
        if fail_at == "construct":
            raise OSError(24, "Too many open files")
        return observer

    watcher = _watcher(comp)
    with patch.object(watcher_mod, "_create_observer", create):
        await watcher.start()
    try:
        expected = (
            "OSError: [Errno 24] Too many open files"
            if fail_at == "construct"
            else "OSError: [Errno 28] inotify watch limit reached"
        )
        assert watcher.observer_error == expected
        assert watcher._observer is None
        if fail_at == "second_schedule":
            assert observer.stop_calls == 1
        await _until_async(lambda: _found(comp, "firstrootword"))
        await _until_async(lambda: _found(comp, "secondrootword"))
    finally:
        await watcher.stop()


@pytest.mark.requires_symlinks
async def test_a_relink_made_while_no_watcher_ran_is_still_hidden(env):
    """The #2904 start check runs even when the observer does not."""
    comp, first, _second = env
    a = first / "a.md"
    a.write_text("## Note\n\nstoredkeyword lives here\n", encoding="utf-8")
    assert (await comp.index_engine.index_file(a)).indexed_chunks == 1
    target = first / "target.md"
    target.write_text("## Other\n\nother\n", encoding="utf-8")
    a.unlink()
    a.symlink_to(target)
    assert await _found(comp, "storedkeyword")

    watcher = _watcher(comp)
    with patch.object(
        watcher_mod, "_create_observer", lambda _c: _RaisingObserver(SystemError(FSEVENTS_MESSAGE))
    ):
        await watcher.start()
    try:
        assert watcher.observer_error == FSEVENTS_ERROR

        async def hidden() -> bool:
            return not await _found(comp, "storedkeyword")

        await _until_async(hidden)
    finally:
        await watcher.stop()
        thread = watcher._relink_scan_thread
        if thread is not None:
            await asyncio.to_thread(thread.join, 10)


async def test_an_observer_that_cannot_be_stopped_is_held_and_bars_a_restart(env):
    comp, _first, _second = env
    observer = _RaisingObserver(SystemError(FSEVENTS_MESSAGE))
    observer.stop_fails = True
    watcher = _watcher(comp)
    with patch.object(watcher_mod, "_create_observer", lambda _c: observer):
        await watcher.start()
    assert watcher._failed_observer is observer
    assert watcher._observer is None

    # A backend swap goes through ``reconfigure``, which must not touch it.
    comp.config.indexing.watcher_backend = "polling"
    await watcher.reconfigure(comp.config.indexing)
    assert watcher._failed_observer is observer
    assert watcher._observer is None

    # Two failed cleanups: the handle stays and start keeps refusing.
    for _ in range(2):
        with pytest.raises(RuntimeError, match="emitter would not stop"):
            await watcher.stop()
        assert watcher._failed_observer is observer
        with pytest.raises(RuntimeError, match="could not be stopped"):
            await watcher.start()

    observer.stop_fails = False
    await watcher.stop()
    assert watcher._failed_observer is None
    with patch.object(watcher_mod, "_create_observer", lambda _c: PollingObserver(timeout=0.05)):
        await watcher.start()
    try:
        assert watcher.live_events is True
        assert watcher.observer_error is None
    finally:
        await watcher.stop()


class _Abort(BaseException):
    pass


async def test_a_base_exception_from_the_observer_still_propagates(env):
    comp, _first, _second = env
    observer = _RaisingObserver(_Abort())
    watcher = _watcher(comp)
    with patch.object(watcher_mod, "_create_observer", lambda _c: observer):
        with pytest.raises(_Abort):
            await watcher.start()
    # Published, so the caller's rollback ``stop`` reaches it.
    assert watcher._observer is observer
    assert watcher.observer_error is None
    await watcher.stop()
    assert observer.stop_calls == 1
    assert watcher._observer is None


async def test_live_events_tracks_the_lifecycle(env):
    comp, _first, _second = env
    watcher = _watcher(comp)
    assert watcher.live_events is False
    with patch.object(
        watcher_mod, "_create_observer", lambda _c: _RaisingObserver(SystemError(FSEVENTS_MESSAGE))
    ):
        await watcher.start()
    assert (watcher.live_events, watcher.observer_error) == (False, FSEVENTS_ERROR)
    await watcher.stop()
    assert watcher.live_events is False
    with patch.object(watcher_mod, "_create_observer", lambda _c: PollingObserver(timeout=0.05)):
        await watcher.start()
    try:
        assert (watcher.live_events, watcher.observer_error) == (True, None)
    finally:
        await watcher.stop()
    assert watcher.live_events is False


# --- the MCP server ---------------------------------------------------------


def _unavailable(report: dict) -> list[dict]:
    return [w for w in report["warnings"] if w["kind"] == "watcher_unavailable"]


async def _initialized(comp, observer_factory) -> AppContext:
    ctx = AppContext(config=comp.config)
    with (
        patch("memtomem.server.component_factory.create_components", AsyncMock(return_value=comp)),
        patch.object(watcher_mod, "_create_observer", observer_factory),
    ):
        await ctx.ensure_initialized()
    return ctx


async def _closed(ctx: AppContext) -> None:
    with patch(
        "memtomem.server.component_factory.close_components",
        AsyncMock(return_value=TeardownResult(storage_closed=True)),
    ):
        await ctx.close()


async def test_the_server_initializes_and_status_warns(env):
    comp, _first, _second = env
    ctx = await _initialized(comp, lambda _c: _RaisingObserver(SystemError(FSEVENTS_MESSAGE)))
    try:
        assert ctx._components is comp
        assert ctx._watcher_started is True
        assert ctx._watcher.live_events is False
        facade = AppContext.from_runtime_owner(ctx)
        await facade.ensure_initialized()
        for surface in (ctx, facade):
            warnings = _unavailable(await collect_status_report(surface))
            assert len(warnings) == 1
            assert warnings[0]["detail"] == (
                f"The file watcher could not start ({FSEVENTS_ERROR}): file edits are "
                "not picked up live; source checks keep running."
            )
            assert warnings[0]["fix"] == (
                'Set "indexing": {"watcher_backend": "polling"} in '
                "~/.memtomem/config.json (an MEMTOMEM_INDEXING__WATCHER_BACKEND "
                "environment variable overrides it), or close other memtomem-server "
                "sessions, then restart the server."
            )
    finally:
        await _closed(ctx)


async def test_a_healthy_server_and_the_cli_do_not_warn(env):
    comp, _first, _second = env
    ctx = await _initialized(comp, lambda _c: PollingObserver(timeout=0.05))
    try:
        assert ctx._watcher.live_events is True
        assert _unavailable(await collect_status_report(ctx)) == []
    finally:
        await _closed(ctx)
    # ``mm status`` builds a context that runs no watcher.
    assert _unavailable(await collect_status_report(AppContext.from_components(comp))) == []


async def test_the_fix_text_backend_is_read_from_config_json(tmp_path, monkeypatch):
    """The key the warning tells the user to set is the one the loader reads."""
    import json

    from memtomem.config import Mem2MemConfig, load_config_overrides

    from helpers import isolate_memtomem_env

    isolate_memtomem_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    (tmp_path / ".memtomem").mkdir()
    (tmp_path / ".memtomem" / "config.json").write_text(
        json.dumps({"indexing": {"watcher_backend": "polling"}}), encoding="utf-8"
    )
    config = Mem2MemConfig()
    load_config_overrides(config)
    assert config.indexing.watcher_backend == "polling"
    assert watcher_mod.effective_watcher_backend(config.indexing) == "polling"


# --- mm web -----------------------------------------------------------------


async def test_mm_web_starts_with_a_watcher_whose_observer_failed(env):
    from fastapi import FastAPI

    from memtomem.web.app import _lifespan

    comp, _first, _second = env
    app = FastAPI()
    with (
        patch("memtomem.server.component_factory.create_components", AsyncMock(return_value=comp)),
        patch("memtomem.server.component_factory.close_components", AsyncMock()),
        patch.object(
            watcher_mod,
            "_create_observer",
            lambda _c: _RaisingObserver(SystemError(FSEVENTS_MESSAGE)),
        ),
    ):
        async with _lifespan(app):
            assert app.state.startup_state == "ready"
            assert app.state.file_watcher_started is True
            assert app.state.file_watcher.observer_error == FSEVENTS_ERROR


def _failed_watcher() -> MagicMock:
    watcher = MagicMock(name="watcher")
    watcher.observer_error = FSEVENTS_ERROR
    return watcher


NOTE = (
    f" The file watcher could not start ({FSEVENTS_ERROR}), so new edits are not"
    " auto-indexed: restart `mm web` to try again."
)


async def test_the_web_reset_does_not_claim_auto_indexing_after_a_resume():
    from memtomem.web.routes.system_config import _resume_watching_locked

    watcher = _failed_watcher()
    state = SimpleNamespace(
        file_watcher=watcher, file_watcher_started=False, file_watcher_resume_blocked=False
    )

    class _Resumer:
        def __init__(self, w, *, started, can_retry):
            self.started, self.can_retry = started, can_retry

        async def resume(self):
            self.started = True
            return True

    assert await _resume_watching_locked(state, _Resumer) == NOTE
    assert state.file_watcher_started is True
    # A repeated reset finds it started: no second start, the same note.
    assert await _resume_watching_locked(state, _Resumer) == NOTE


async def test_the_web_reset_messages_are_unchanged_for_a_working_watcher():
    from memtomem.web.routes.system_config import _resume_watching_locked

    watcher = MagicMock(name="watcher")
    watcher.observer_error = None
    state = SimpleNamespace(
        file_watcher=watcher, file_watcher_started=True, file_watcher_resume_blocked=False
    )
    assert await _resume_watching_locked(state, MagicMock()) == ""


def test_the_partial_start_emitter_threads_do_not_leak():
    """Guard for the fake itself: no emitter thread outlives the module."""
    leftovers = [t for t in threading.enumerate() if isinstance(t, _SecondStartFails)]
    assert all(not t.is_alive() for t in leftovers)
