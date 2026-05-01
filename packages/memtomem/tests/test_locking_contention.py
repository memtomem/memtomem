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

import multiprocessing as mp
import time
from pathlib import Path

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

        # 50ms scheduler slack: p2 must not have entered before p1 left.
        assert p2_acquired >= p1_released - 0.05, (
            f"p2 acquired {p2_acquired} before p1 released {p1_released}"
        )
        # And p2 was actually blocked for most of the hold window.
        assert (p2_acquired - p2_requested) >= hold_seconds * 0.5

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
