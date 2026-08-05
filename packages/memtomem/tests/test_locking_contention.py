"""Cross-platform lock contention tests for the portalocker swap (#625).

Covers the three sites that switched from ``fcntl``/``msvcrt`` to
``portalocker``:

- ``context._atomic._file_lock`` (sidecar lockfile pattern, atomic writes)
- ``indexing.debounce._Lock`` (sidecar lockfile pattern, debounce queue)
- ``cli._liveness.probe_pid_file`` (probe holder of server PID lock)

No ``skipif(win32)`` — the whole point of #625 is that these now serialize
on every supported OS, replacing the prior msvcrt-branch / Windows-no-op /
conservative assume-alive fallbacks.

Tests use ``multiprocessing`` (not threads) because portalocker delegates
to ``fcntl.flock`` / ``LockFileEx``, both of which are process-level — same
process holding two refs would not contend.

Each worker gets its own ``mp.Queue`` (rather than sharing one). Python's
multiprocessing docs guarantee FIFO order *only within a single producer*;
items put by different processes can interleave in the receiver's view,
which on slower runners flips the order between p1's "released" and p2's
"acquired". Per-process queues keep the within-queue ordering meaningful;
cross-process ordering is verified separately via timestamps.
"""

from __future__ import annotations

import contextlib
import multiprocessing as mp
import time
from pathlib import Path

import pytest

# spawn: cross-platform consistency (Windows + macOS default since Py 3.8;
# Linux otherwise forks, which is fine but spawn keeps test semantics
# uniform across CI matrix rows).
_CTX = mp.get_context("spawn")


# ----------------------------------------------------------------- helpers


def _hold_atomic_lock(lock_path_str: str, hold_seconds: float, q) -> None:
    """Take ``_file_lock`` and hold it for ``hold_seconds``."""
    from memtomem.context._atomic import _file_lock

    with _file_lock(Path(lock_path_str)):
        q.put(("acquired", time.monotonic()))
        time.sleep(hold_seconds)
        q.put(("released", time.monotonic()))


def _take_atomic_lock(lock_path_str: str, q) -> None:
    """Try to take ``_file_lock``; record request and acquire timestamps."""
    from memtomem.context._atomic import _file_lock

    q.put(("requested", time.monotonic()))
    with _file_lock(Path(lock_path_str)):
        q.put(("acquired", time.monotonic()))


def _hold_debounce_lock(queue_path_str: str, hold_seconds: float, q) -> None:
    from memtomem.indexing.debounce import _Lock

    with _Lock(Path(queue_path_str)):
        q.put(("acquired", time.monotonic()))
        time.sleep(hold_seconds)
        q.put(("released", time.monotonic()))


def _take_debounce_lock(queue_path_str: str, q) -> None:
    from memtomem.indexing.debounce import _Lock

    q.put(("requested", time.monotonic()))
    with _Lock(Path(queue_path_str)):
        q.put(("acquired", time.monotonic()))


def _hold_pid_lock_via_portalocker(pid_file_str: str, hold_seconds: float, q) -> None:
    """Stand in for ``server/__init__.py:main`` — hold an exclusive
    portalocker lock on the pid file so ``probe_pid_file`` sees a writer."""
    import portalocker

    pid_file = Path(pid_file_str)
    pid_file.write_text("4242", encoding="utf-8")
    fp = open(pid_file, "rb+")
    try:
        portalocker.lock(fp, portalocker.LOCK_EX)
        q.put("locked")
        time.sleep(hold_seconds)
        portalocker.unlock(fp)
    finally:
        fp.close()
    q.put("released")


# --------------------------------------------------------- _file_lock


class TestAtomicLockContention:
    def test_second_process_blocks_until_first_releases(self, tmp_path: Path):
        """Positive pin: two processes contending on the same sidecar lock
        serialize — the second's acquisition is >= the first's release."""
        lock_path = tmp_path / ".guard.lock"
        q1 = _CTX.Queue()
        q2 = _CTX.Queue()
        hold_seconds = 0.5

        p1 = _CTX.Process(target=_hold_atomic_lock, args=(str(lock_path), hold_seconds, q1))
        p1.start()
        msg, p1_acquired = q1.get(timeout=10)
        assert msg == "acquired"

        p2 = _CTX.Process(target=_take_atomic_lock, args=(str(lock_path), q2))
        p2.start()
        msg, p2_requested = q2.get(timeout=10)
        assert msg == "requested"

        msg, p1_released = q1.get(timeout=10)
        assert msg == "released"
        msg, p2_acquired = q2.get(timeout=10)
        assert msg == "acquired"

        p1.join(timeout=5)
        p2.join(timeout=5)
        assert p1.exitcode == 0
        assert p2.exitcode == 0

        # Phase-ordering pin (jitter-immune): p2 made its request while p1
        # still held the lock, and only acquired once p1 had released. The
        # earlier ``(p2_acquired - p2_requested) >= hold_seconds * 0.5``
        # check measured wall-clock wait and flaked on macOS CI when
        # ``Process.start()`` jitter pushed p2_requested past p1's hold
        # midpoint — the lock was working, the duration just didn't make
        # the cutoff (#821).
        assert p2_requested < p1_released, (
            f"p2 requested {p2_requested} after p1 released {p1_released} — "
            "spawn jitter exceeded hold window; the lock can't be proven "
            "to have blocked p2 in this run"
        )
        # 50ms scheduler slack: p2 must not have entered before p1 left.
        assert p2_acquired >= p1_released - 0.05, (
            f"p2 acquired {p2_acquired} before p1 released {p1_released}"
        )

    def test_uncontended_acquire_is_immediate(self, tmp_path: Path):
        """Negative pin: with no holder, _file_lock acquires without
        meaningful blocking — pairs with the contention pin to prove
        the assertion above is symmetric (lock works AND lock blocks)."""
        lock_path = tmp_path / ".guard.lock"
        q = _CTX.Queue()
        p = _CTX.Process(target=_take_atomic_lock, args=(str(lock_path), q))
        p.start()
        msg, requested = q.get(timeout=10)
        msg, acquired = q.get(timeout=10)
        p.join(timeout=5)
        assert p.exitcode == 0
        assert (acquired - requested) < 1.0


# ----------------------------------------------------- _Lock (debounce)


class TestDebounceLockContention:
    def test_second_process_blocks_until_first_releases(self, tmp_path: Path):
        """Replaces the prior 'POSIX only; on Windows the lock is a no-op'
        contract — debounce queue mutators now serialize on every OS."""
        queue_path = tmp_path / "debounce_queue.json"
        q1 = _CTX.Queue()
        q2 = _CTX.Queue()
        hold_seconds = 0.5

        p1 = _CTX.Process(target=_hold_debounce_lock, args=(str(queue_path), hold_seconds, q1))
        p1.start()
        msg, p1_acquired = q1.get(timeout=10)
        assert msg == "acquired"

        p2 = _CTX.Process(target=_take_debounce_lock, args=(str(queue_path), q2))
        p2.start()
        msg, p2_requested = q2.get(timeout=10)
        assert msg == "requested"

        msg, p1_released = q1.get(timeout=10)
        assert msg == "released"
        msg, p2_acquired = q2.get(timeout=10)
        assert msg == "acquired"

        p1.join(timeout=5)
        p2.join(timeout=5)
        assert p1.exitcode == 0
        assert p2.exitcode == 0

        assert p2_acquired >= p1_released - 0.05


# ------------------------------------------------- probe_pid_file


class TestLivenessProbeContention:
    def test_probe_returns_alive_when_holder_exists(self, tmp_path: Path):
        """Cross-platform pin: ``probe_pid_file`` detects a live writer on
        every OS, replacing the prior conservative assume-alive Windows
        fallback (#448 → #625).

        The pid value is best-effort: POSIX flock is advisory and the
        probe can read the pid alongside the holder, but on Windows
        ``LockFileEx``'s mandatory exclusive lock blocks reads of the
        locked byte range. The production code degrades gracefully to
        ``pid=None`` in that case (the user-facing message just says
        "server alive" without a pid). The assertion here therefore
        accepts both — the contract is ``alive=True``, not a specific
        pid value.
        """
        from memtomem.cli._liveness import probe_pid_file

        pid_file = tmp_path / "server.pid"
        q = _CTX.Queue()
        p = _CTX.Process(target=_hold_pid_lock_via_portalocker, args=(str(pid_file), 1.0, q))
        p.start()
        try:
            assert q.get(timeout=10) == "locked"

            state = probe_pid_file(pid_file)
            assert state.alive is True
            # Windows mandatory lock blocks the read; pid may be None.
            assert state.pid in (4242, None)
            assert state.pid_file == pid_file

            assert q.get(timeout=10) == "released"
        finally:
            p.join(timeout=5)
            assert p.exitcode == 0

    def test_probe_returns_dead_when_no_holder(self, tmp_path: Path):
        """Negative pin: a stale pid file with no live writer probes as
        dead — the lock is acquirable, so the recorded PID is gone.

        Doubles as a regression pin for the Windows ``msvcrt.locking``
        access-mode trap: read-only file handles fail with ``EACCES``
        regardless of contention, which would make every probe return
        ``alive=True`` on Windows. ``probe_pid_file`` therefore opens
        the pid file ``"rb+"`` (R/W). If a future refactor weakens that
        to ``"rb"``, this test fails on Windows because lock acquire
        becomes unreachable on an uncontended file.
        """
        from memtomem.cli._liveness import probe_pid_file

        pid_file = tmp_path / "server.pid"
        pid_file.write_text("4242", encoding="utf-8")

        state = probe_pid_file(pid_file)
        assert state.alive is False
        assert state.pid == 4242


# ------------------------------------------- check_server_liveness (#1990)


class TestCheckServerLivenessStoreScope:
    """The liveness aggregate is store-scoped: with a ``db_path`` it must
    see this store's ``server-<digest>.pid`` and the transitional/legacy
    names, and must NOT report a *foreign* store's live server (#1990).

    In-process ``portalocker`` holders are sufficient here: ``flock`` /
    ``LockFileEx`` contention is per open handle, and the uninstall suite
    already relies on the same pattern (``_hold_pid_lock``).
    """

    @pytest.fixture()
    def rt(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """Isolated runtime dir + HOME so probes never touch real state."""
        import os
        import tempfile

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        xdg = tmp_path / "xdg"
        xdg.mkdir()
        if os.name != "nt":
            os.chmod(xdg, 0o700)
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(xdg))
        tmp_tmp = tmp_path / "tmp"
        tmp_tmp.mkdir()
        monkeypatch.setattr(tempfile, "tempdir", str(tmp_tmp))

        from memtomem._runtime_paths import ensure_runtime_dir

        return ensure_runtime_dir()

    @contextlib.contextmanager
    def _hold(self, pid_file: Path):
        import portalocker

        pid_file.parent.mkdir(parents=True, exist_ok=True)
        if not pid_file.exists():
            pid_file.write_text("4242", encoding="utf-8")
        fp = open(pid_file, "rb+")
        try:
            portalocker.lock(fp, portalocker.LOCK_EX | portalocker.LOCK_NB)
            yield
        finally:
            try:
                portalocker.unlock(fp)
            except Exception:
                pass
            fp.close()

    def test_scoped_probe_sees_own_store_holder(self, rt: Path, tmp_path: Path):
        from memtomem._runtime_paths import server_pid_path
        from memtomem.cli._liveness import check_server_liveness

        db = tmp_path / "store" / "memtomem.db"
        with self._hold(rt / server_pid_path(db).name):
            state = check_server_liveness(db)

        assert state.alive is True
        assert state.pid_file is not None and state.pid_file.name == server_pid_path(db).name

    def test_scoped_probe_ignores_foreign_store_holder(self, rt: Path, tmp_path: Path):
        from memtomem._runtime_paths import server_pid_path
        from memtomem.cli._liveness import check_server_liveness

        mine = tmp_path / "store" / "memtomem.db"
        other = tmp_path / "other" / "memtomem.db"
        with self._hold(rt / server_pid_path(other).name):
            state = check_server_liveness(mine)

        assert state.alive is False, (
            "a live server on a different store must not gate work on this one"
        )

    def test_scoped_probe_still_sees_transitional_bare_holder(self, rt: Path, tmp_path: Path):
        from memtomem.cli._liveness import check_server_liveness

        db = tmp_path / "store" / "memtomem.db"
        with self._hold(rt / "server.pid"):
            state = check_server_liveness(db)

        assert state.alive is True, (
            "a pre-#1990 server's bare server.pid cannot be attributed to a "
            "store and must keep refusing fail-closed"
        )

    def test_store_agnostic_probe_globs_scoped_files(self, rt: Path, tmp_path: Path):
        from memtomem._runtime_paths import server_pid_path
        from memtomem.cli._liveness import check_server_liveness

        db = tmp_path / "store" / "memtomem.db"
        with self._hold(rt / server_pid_path(db).name):
            state = check_server_liveness()

        assert state.alive is True, (
            "the no-db_path arm (mm upgrade) must find per-store pid files via glob"
        )

    def test_store_agnostic_enumerator_returns_every_live_holder(self, rt: Path, tmp_path: Path):
        from memtomem._runtime_paths import server_pid_path
        from memtomem.cli._liveness import (
            check_server_liveness,
            enumerate_server_liveness,
        )

        store_a = tmp_path / "a" / "memtomem.db"
        store_b = tmp_path / "b" / "memtomem.db"
        pid_a = rt / server_pid_path(store_a).name
        pid_b = rt / server_pid_path(store_b).name
        stale = rt / "server-ffffffffffffffff.pid"
        pid_a.write_text("111", encoding="utf-8")
        pid_b.write_text("222", encoding="utf-8")
        stale.write_text("333", encoding="utf-8")

        with self._hold(pid_b), self._hold(pid_a):
            states = enumerate_server_liveness()
            aggregate = check_server_liveness()

        expected = sorted((pid_a, pid_b))
        assert [state.pid_file for state in states] == expected
        assert {state.pid for state in states if state.pid is not None} <= {111, 222}
        assert aggregate.pid_file == expected[0]
        assert stale not in [state.pid_file for state in states]

    def test_store_agnostic_probe_fails_closed_on_glob_error(
        self, rt: Path, monkeypatch: pytest.MonkeyPatch
    ):
        import memtomem.cli._liveness as liveness

        class _Unsearchable:
            def glob(self, pattern: str):
                raise OSError("unsearchable runtime dir")

            def __str__(self) -> str:
                return "<runtime dir>"

        monkeypatch.setattr(liveness, "runtime_dir", lambda: _Unsearchable())

        states = liveness.enumerate_server_liveness()
        state = liveness.check_server_liveness()

        assert len(states) == 1
        assert states[0].alive is True
        assert states[0].probe_error is not None
        assert state.alive is True
        assert state.probe_error is not None, (
            "an unenumerable runtime dir is an assumption, not observed "
            "evidence — probe_error must say so (#1949)"
        )
