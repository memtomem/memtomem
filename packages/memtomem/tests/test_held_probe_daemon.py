"""The held recheck's probe runs on a daemon thread, so a stalled one cannot block exit (#2908).

The probe used to run in the default executor. Its timeout stopped the wait but
not the thread, and ``asyncio.run`` and interpreter exit both join executor
threads, so a ``stat`` stalled on an unresponsive mount kept the MCP server or
``mm web`` from exiting. ``FileWatcher`` is driven over a stub storage here.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import textwrap
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from memtomem.config import IndexingConfig
from memtomem.indexing import watcher as watcher_mod
from memtomem.indexing.watcher import FileWatcher


class _Store:
    def __init__(self, held: list[Path]) -> None:
        self.held = held

    async def get_held_sources(self) -> list[Path]:
        return list(self.held)

    async def get_pending_source_checks(self) -> list[Path]:
        return []

    async def is_source_held(self, path: Path) -> bool:
        return True


def _watcher(tmp_path: Path, held: list[Path]) -> FileWatcher:
    engine = SimpleNamespace(_storage=_Store(held))
    return FileWatcher(engine, IndexingConfig(memory_dirs=[tmp_path]))


def test_a_stalled_probe_does_not_hold_up_process_exit(tmp_path):
    script = textwrap.dedent(
        """
        import asyncio
        import threading
        from pathlib import Path
        from types import SimpleNamespace

        from memtomem.config import IndexingConfig
        from memtomem.indexing import watcher as watcher_mod
        from memtomem.indexing.watcher import FileWatcher
        from memtomem.storage import orphan_detect

        class Store:
            async def get_held_sources(self):
                return [Path(KEY)]

            async def get_pending_source_checks(self):
                return []

            async def is_source_held(self, path):
                return True

        started = threading.Event()

        def stalled(path):
            print("probe-started", flush=True)
            started.set()
            threading.Event().wait()

        orphan_detect.source_state = stalled
        watcher_mod._HELD_PROBE_TIMEOUT_S = 0.1

        async def main():
            engine = SimpleNamespace(_storage=Store())
            watcher = FileWatcher(engine, IndexingConfig(memory_dirs=[Path(KEY).parent]))
            task = asyncio.create_task(watcher._recheck_held_loop())
            for _ in range(250):
                if started.is_set():
                    break
                await asyncio.sleep(0.02)
            await asyncio.sleep(0.3)  # past the probe's timeout
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
    # The probe really stalled, and the process still exited.
    assert proc.stdout.split() == ["probe-started", "exited"]


def test_a_probe_failing_after_its_timeout_is_not_reported_unretrieved(tmp_path):
    """Once the wait for a probe times out, ``shield`` drops its callback, so a
    probe that raises later has no one to retrieve its exception. Unretrieved,
    asyncio logs "Future exception was never retrieved" when the future is
    collected. Checked in a child, where the loop and every reference can go."""
    script = textwrap.dedent(
        """
        import asyncio
        import gc
        import logging
        import threading
        from pathlib import Path
        from types import SimpleNamespace

        from memtomem.config import IndexingConfig
        from memtomem.indexing import watcher as watcher_mod
        from memtomem.indexing.watcher import FileWatcher

        reports = []

        class Grab(logging.Handler):
            def emit(self, record):
                if "never retrieved" in record.getMessage():
                    reports.append(record.getMessage())

        logging.getLogger("asyncio").addHandler(Grab())
        release = threading.Event()
        raised = threading.Event()

        def probe(path):
            release.wait(10)
            raised.set()
            raise OSError("mount went away")

        watcher_mod._held_probe = probe
        watcher_mod._HELD_PROBE_TIMEOUT_S = 0.05

        class Store:
            async def get_held_sources(self):
                return [Path(KEY)]

            async def get_pending_source_checks(self):
                return []

            async def is_source_held(self, path):
                return True

        async def main():
            engine = SimpleNamespace(_storage=Store())
            watcher = FileWatcher(engine, IndexingConfig(memory_dirs=[Path(KEY).parent]))
            task = asyncio.create_task(watcher._recheck_held_loop())
            for _ in range(250):
                if watcher._stalled_probes:
                    break
                await asyncio.sleep(0.02)
            await asyncio.sleep(0.2)  # past the timeout: nobody awaits the probe now
            release.set()
            raised.wait(5)
            for _ in range(250):
                if not watcher._stalled_probes:
                    break
                await asyncio.sleep(0.02)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            del watcher, task
            gc.collect()
            await asyncio.sleep(0)

        asyncio.run(main())
        gc.collect()
        print("raised" if raised.is_set() else "not-raised", len(reports))
        """
    ).replace("KEY", repr(str(tmp_path / "a.md")))

    proc = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.split() == ["raised", "0"], proc.stderr


@pytest.mark.asyncio
async def test_two_stalled_probes_pause_the_recheck_until_one_returns(
    tmp_path, monkeypatch, caplog
):
    held = [tmp_path / f"{name}.md" for name in "abc"]
    release = threading.Event()
    probed: list[Path] = []

    def probe(path: Path) -> tuple[str, bool]:
        probed.append(path)
        release.wait(10)
        return "missing", False

    monkeypatch.setattr(watcher_mod, "_held_probe", probe)
    monkeypatch.setattr(watcher_mod, "_HELD_PROBE_TIMEOUT_S", 0.05)
    watcher = _watcher(tmp_path, held)
    task = asyncio.create_task(watcher._recheck_held_loop())
    try:
        for _ in range(100):
            if "Held-source probes stalled" in caplog.text:
                break
            await asyncio.sleep(0.02)

        assert "Held-source probes stalled" in caplog.text
        # The third path waits: two stalled probes are the budget.
        assert probed == held[:2]
        assert len(watcher._stalled_probes) == 2

        release.set()
        for _ in range(100):
            if not watcher._stalled_probes and len(probed) == 3:
                break
            await asyncio.sleep(0.02)

        # The returned probes left the stall set, and the recheck went on.
        assert held[2] in probed
        assert not watcher._stalled_probes
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_probe_that_raises_reaches_the_recheck_as_before(tmp_path, monkeypatch, caplog):
    """The thread hands the exception back, as the executor did; it is not left pending."""

    def probe(path: Path) -> tuple[str, bool]:
        raise OSError("mount went away")

    monkeypatch.setattr(watcher_mod, "_held_probe", probe)
    watcher = _watcher(tmp_path, [tmp_path / "a.md"])
    task = asyncio.create_task(watcher._recheck_held_loop())
    try:
        for _ in range(100):
            if "Held-source recheck failed" in caplog.text:
                break
            await asyncio.sleep(0.02)

        assert "Held-source recheck failed" in caplog.text
        assert "mount went away" in caplog.text
        assert not watcher._stalled_probes
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_a_probe_outliving_its_event_loop_finishes_quietly(tmp_path, monkeypatch):
    """The thread hands its result to a loop that has closed; that is dropped, not raised."""
    release = threading.Event()

    def probe(path: Path) -> tuple[str, bool]:
        release.wait(10)
        return "missing", False

    monkeypatch.setattr(watcher_mod, "_held_probe", probe)
    raised: list[BaseException] = []
    monkeypatch.setattr(threading, "excepthook", lambda args: raised.append(args.exc_value))
    threads: list[threading.Thread] = []
    real_thread = threading.Thread

    def recording_thread(*args, **kwargs):
        thread = real_thread(*args, **kwargs)
        threads.append(thread)
        return thread

    monkeypatch.setattr(watcher_mod.threading, "Thread", recording_thread)
    loop = asyncio.new_event_loop()
    try:

        async def start() -> None:
            watcher_mod._start_held_probe(tmp_path / "a.md")

        loop.run_until_complete(start())
    finally:
        loop.close()
    assert len(threads) == 1 and threads[0].daemon
    release.set()
    threads[0].join(5)

    assert not threads[0].is_alive()
    assert raised == []
