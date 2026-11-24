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


class _Probes:
    """A held probe whose paths in ``gates`` block until their event is set (#3263).

    ``install`` notes every probe thread on the loop thread as it is created,
    with its path: which paths were probed does not depend on when a worker
    gets CPU time, and teardown can join them all. It reads neither what
    ``_start_held_probe`` returns nor the stall set's type, so these tests run
    unchanged against the code before the fix.
    """

    def __init__(self, *blocking: Path) -> None:
        self.gates = {path: threading.Event() for path in blocking}
        self.started: list[tuple[Path, threading.Thread]] = []
        self._created: list[threading.Thread] = []

    def __call__(self, path: Path) -> tuple[str, bool]:
        gate = self.gates.get(path)
        if gate is not None:
            # No timeout: a blocked probe must stay blocked however slow the
            # runner is. The fixture's teardown sets every gate.
            gate.wait()
        return "missing", False

    def install(self, monkeypatch) -> None:
        monkeypatch.setattr(watcher_mod, "_held_probe", self)
        real_thread = threading.Thread
        created = self._created

        def recording_thread(*args, **kwargs):
            thread = real_thread(*args, **kwargs)
            if kwargs.get("name") == "memtomem-held-probe":
                created.append(thread)
            return thread

        monkeypatch.setattr(watcher_mod.threading, "Thread", recording_thread)
        start = watcher_mod._start_held_probe

        def started(path: Path):
            before = len(created)
            result = start(path)
            (thread,) = created[before:]
            self.started.append((path, thread))
            return result

        monkeypatch.setattr(watcher_mod, "_start_held_probe", started)

    @property
    def paths(self) -> list[Path]:
        return [path for path, _ in self.started]

    def threads(self, path: Path) -> list[threading.Thread]:
        return [thread for probed, thread in self.started if probed == path]

    def release(self, path: Path) -> None:
        self.gates[path].set()
        for thread in self.threads(path):
            thread.join(10)
            assert not thread.is_alive()

    def release_all(self) -> None:
        for gate in self.gates.values():
            gate.set()
        for _, thread in self.started:
            thread.join(10)
        assert not [path for path, thread in self.started if thread.is_alive()]


async def _until(condition, timeout: float = 10.0) -> bool:
    """Poll ``condition``; the timeout only guards against a hang."""
    for _ in range(int(timeout / 0.01)):
        if condition():
            return True
        await asyncio.sleep(0.01)
    return condition()


_STALLED = "Held-source probes stalled"


def _run_recheck(watcher: FileWatcher, body) -> None:
    """Run the held recheck in a fresh ``asyncio.run`` while ``body`` drives it."""

    async def main() -> None:
        task = asyncio.create_task(watcher._recheck_held_loop())
        try:
            await body()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(main())


def _stall_two_probes_on_a_closed_loop(watcher, store, probes, a, b, caplog) -> None:
    """Leave the probes of ``a`` and ``b`` timed out on a loop that then closes."""
    sentinel = a.parent / "sentinel.md"
    store.held = [a, b, sentinel]

    async def body() -> None:
        assert await _until(lambda: _STALLED in caplog.text)
        # The third path waits behind the two stalled probes.
        assert probes.paths == [a, b]
        assert len(watcher._stalled_probes) == 2

    _run_recheck(watcher, body)
    caplog.clear()


@pytest.fixture
def two_loops(tmp_path, monkeypatch):
    monkeypatch.setattr(watcher_mod, "_HELD_PROBE_TIMEOUT_S", 0.05)
    monkeypatch.setattr(watcher_mod, "_HELD_STALL_POLL_S", 0.05, raising=False)
    a, b, c, d = (tmp_path / f"{name}.md" for name in "abcd")
    watcher = _watcher(tmp_path, [])
    store = watcher._engine._storage
    probes = _Probes(a, b, c)
    probes.install(monkeypatch)
    try:
        yield watcher, store, probes, (a, b, c, d)
    finally:
        probes.release_all()


def test_probes_stranded_by_a_closed_loop_do_not_stall_the_next_loop(two_loops, caplog):
    """A probe that timed out on a loop that closed is never settled; its finished
    thread must not keep counting against the next loop's budget (#3263)."""
    watcher, store, probes, (a, b, c, _) = two_loops
    _stall_two_probes_on_a_closed_loop(watcher, store, probes, a, b, caplog)
    probes.release(a)
    probes.release(b)
    probes.gates.pop(c)  # ``c`` answers at once on the new loop.
    store.held = [c]

    async def body() -> None:
        # ``c`` was probed, and its answer settled it, so nothing is counted.
        assert await _until(lambda: c in probes.paths and not watcher._stalled_probes), caplog.text

    _run_recheck(watcher, body)
    assert _STALLED not in caplog.text


def test_a_probe_still_stalled_from_a_closed_loop_keeps_counting_until_it_ends(two_loops, caplog):
    """A thread still blocked from the earlier loop holds its slot, so a new loop
    does not pile probes behind a dead mount, and the slot frees when it ends."""
    watcher, store, probes, (a, b, c, _) = two_loops
    _stall_two_probes_on_a_closed_loop(watcher, store, probes, a, b, caplog)
    probes.gates.pop(c)
    store.held = [c]

    async def body() -> None:
        assert await _until(lambda: _STALLED in caplog.text)
        assert c not in probes.paths
        probes.release(a)
        # ``c`` is probed and settles; ``b`` is still blocked and still counted.
        assert await _until(
            lambda: (
                c in probes.paths and list(watcher._stalled_probes.values()) == probes.threads(b)
            )
        )
        assert all(thread.is_alive() for thread in probes.threads(b))

    _run_recheck(watcher, body)


def test_a_stall_shared_with_an_earlier_loop_is_noticed_while_an_own_probe_waits(two_loops, caplog):
    """One stalled thread from the closed loop and one probe of this loop: the wait
    on this loop's probe must not hide the other thread's end for the full interval."""
    watcher, store, probes, (a, b, c, d) = two_loops
    _stall_two_probes_on_a_closed_loop(watcher, store, probes, a, b, caplog)
    probes.release(a)
    store.held = [c, d]

    async def body() -> None:
        assert await _until(lambda: _STALLED in caplog.text)
        # ``b`` from the closed loop and ``c`` from this one fill the budget.
        assert probes.paths[-1] == c
        assert d not in probes.paths
        probes.release(b)
        assert await _until(lambda: d in probes.paths)
        assert all(thread.is_alive() for thread in probes.threads(c))

    _run_recheck(watcher, body)
