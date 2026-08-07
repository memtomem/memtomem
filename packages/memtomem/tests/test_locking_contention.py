"""Cross-platform lock contention tests for the portalocker swap (#625).

Covers the three sites that switched from ``fcntl``/``msvcrt`` to
``portalocker``:

- ``context._atomic._file_lock`` (sidecar lockfile pattern, atomic writes)
- ``indexing.debounce._Lock`` (sidecar lockfile pattern, debounce queue)
- ``cli._liveness.probe_pid_file`` (probe holder of server PID lock)

No ``skipif(win32)`` — the whole point of #625 is that these now serialize
on every supported OS, replacing the prior msvcrt-branch / Windows-no-op /
conservative assume-alive fallbacks.

Most tests use ``multiprocessing`` because the production contract is
cross-process serialization. Independent handles in one process can also
contend, and the Web stop test uses that narrower shape to exercise the real
liveness probe while a fake signal releases the held lock.

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
import os
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

    def test_probe_parses_bounded_three_line_payload(self, tmp_path: Path):
        from memtomem.cli._liveness import probe_pid_file

        pid_file = tmp_path / "web.pid"
        pid_file.write_text("4242\n18080\n2026-08-05T10:00:00+00:00\n", encoding="utf-8")

        state = probe_pid_file(pid_file)

        assert state.alive is False
        assert state.pid == 4242
        assert state.port == 18080
        assert state.started == "2026-08-05T10:00:00+00:00"

    @pytest.mark.parametrize(
        ("payload_size", "expected_pid"),
        [(4096, 4242), (4097, None)],
    )
    def test_pid_payload_limit_exact_boundary(
        self, tmp_path: Path, payload_size: int, expected_pid: int | None
    ):
        from memtomem.cli._liveness import probe_pid_file

        prefix = b"4242\n"
        pid_file = tmp_path / "server.pid"
        pid_file.write_bytes(prefix + b"x" * (payload_size - len(prefix)))

        state = probe_pid_file(pid_file)

        assert state.alive is False
        assert state.pid == expected_pid

    @pytest.mark.requires_symlinks
    def test_probe_rejects_symlink_without_reading_target(self, tmp_path: Path):
        from memtomem.cli._liveness import probe_pid_file

        target = tmp_path / "unrelated.lock"
        target.write_text("987654\n", encoding="utf-8")
        link = tmp_path / "server.pid"
        link.symlink_to(target)

        state = probe_pid_file(link)

        assert state.alive is True
        assert state.pid is None, "a symlink target must never supply a signalable PID"
        assert state.pid_file == link
        assert state.probe_error is not None

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO creation is POSIX-only")
    def test_probe_rejects_fifo_without_blocking(self, tmp_path: Path):
        from memtomem.cli._liveness import probe_pid_file

        fifo = tmp_path / "server.pid"
        os.mkfifo(fifo)

        started = time.monotonic()
        state = probe_pid_file(fifo)

        assert time.monotonic() - started < 1.0
        assert state.alive is True
        assert state.pid is None
        assert state.probe_error is not None

    @pytest.mark.skipif(os.name == "nt", reason="POSIX device-node coverage")
    def test_probe_rejects_device_node(self):
        from memtomem.cli._liveness import probe_pid_file

        state = probe_pid_file(Path("/dev/null"))

        assert state.alive is True
        assert state.pid is None
        assert state.probe_error is not None

    def test_oversized_live_payload_never_exposes_pid(self, tmp_path: Path, monkeypatch):
        import portalocker

        from memtomem.cli import _liveness

        pid_file = tmp_path / "server.pid"
        pid_file.write_bytes(b"987654\n" + b"x" * _liveness._PID_PAYLOAD_MAX_BYTES)

        def _already_locked(*_args, **_kwargs):
            raise portalocker.AlreadyLocked("held")

        monkeypatch.setattr(_liveness.portalocker, "lock", _already_locked)
        state = _liveness.probe_pid_file(pid_file)

        assert state.alive is True
        assert state.pid is None, "an oversized valid-looking prefix must not be parsed"
        assert state.probe_error is None, "observed lock contention remains authoritative"

    def test_invalid_utf8_metadata_does_not_break_stale_probe(self, tmp_path: Path):
        from memtomem.cli._liveness import probe_pid_file

        pid_file = tmp_path / "server.pid"
        pid_file.write_bytes(b"4242\n\xff\n")

        state = probe_pid_file(pid_file)

        assert state.alive is False
        assert state.pid is None

    def test_post_lock_identity_failure_discards_pid(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from memtomem.cli import _liveness

        pid_file = tmp_path / "server.pid"
        pid_file.write_text("987654\n", encoding="utf-8")
        real_verify = _liveness._verify_opened_regular
        calls = 0

        def _verify(fd, path, path_stat, parent_stat):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise _liveness._UnsafeProbePathError("simulated inode replacement")
            return real_verify(fd, path, path_stat, parent_stat)

        monkeypatch.setattr(_liveness, "_verify_opened_regular", _verify)
        state = _liveness.probe_pid_file(pid_file)

        assert calls == 2
        assert state.alive is True
        assert state.pid is None
        assert state.probe_error is not None


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

    @contextlib.contextmanager
    def _hold_shared(self, pid_file: Path):
        import portalocker

        pid_file.parent.mkdir(parents=True, exist_ok=True)
        if not pid_file.exists():
            pid_file.write_text("4242", encoding="utf-8")
        fp = open(pid_file, "rb+")
        try:
            portalocker.lock(fp, portalocker.LOCK_SH | portalocker.LOCK_NB)
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

    def test_store_agnostic_compat_probe_still_short_circuits(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        import memtomem.cli._liveness as liveness

        first = tmp_path / "server-aaaaaaaaaaaaaaaa.pid"
        second = tmp_path / "server-bbbbbbbbbbbbbbbb.pid"
        seen: list[Path] = []
        monkeypatch.setattr(liveness, "_glob_server_pid_files", lambda: ([first, second], "rt"))

        def probe(path: Path):
            seen.append(path)
            return liveness.ServerState(alive=True, pid=111, pid_file=path)

        monkeypatch.setattr(liveness, "probe_pid_file", probe)

        state = liveness.check_server_liveness()

        assert state.pid_file == first
        assert seen == [first]

    def test_store_agnostic_enumerator_returns_every_live_holder(self, rt: Path, tmp_path: Path):
        from memtomem._runtime_paths import server_pid_path
        from memtomem.cli._liveness import (
            check_server_liveness,
            enumerate_server_liveness_inventory,
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
            states, warning = enumerate_server_liveness_inventory()
            aggregate = check_server_liveness()

        expected = sorted((pid_a, pid_b))
        assert [state.pid_file for state in states] == expected
        if os.name != "nt":
            assert {state.pid for state in states} == {111, 222}
        else:
            assert all(state.pid in {111, 222, None} for state in states)
        assert aggregate.pid_file == expected[0]
        assert stale not in [state.pid_file for state in states]
        assert warning is None

    @pytest.mark.skipif(os.name == "nt", reason="legacy shared flock is POSIX-only")
    def test_legacy_probe_distinguishes_shared_alias_from_exclusive_holder(self, tmp_path: Path):
        from memtomem.cli._liveness import probe_legacy_pid_file

        legacy = tmp_path / ".server.pid"
        with self._hold_shared(legacy):
            shared = probe_legacy_pid_file(legacy)
        with self._hold(legacy):
            exclusive = probe_legacy_pid_file(legacy)

        assert shared.alive is True
        assert shared.legacy_lock_mode == "shared"
        assert exclusive.alive is True
        assert exclusive.legacy_lock_mode == "exclusive"

    @pytest.mark.skipif(os.name == "nt", reason="legacy shared flock is POSIX-only")
    def test_legacy_contended_probe_discards_pid_after_identity_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        import portalocker

        from memtomem.cli import _liveness

        legacy = tmp_path / ".server.pid"
        legacy.write_text("987654\n", encoding="utf-8")
        monkeypatch.setattr(
            _liveness,
            "probe_pid_file",
            lambda _path: _liveness.ServerState(
                alive=True,
                pid=987654,
                pid_file=legacy,
            ),
        )

        real_verify = _liveness._verify_opened_regular
        calls = 0

        def _verify(fd, path, path_stat, parent_stat):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise _liveness._UnsafeProbePathError("simulated inode replacement")
            return real_verify(fd, path, path_stat, parent_stat)

        def _already_locked(*_args, **_kwargs):
            raise portalocker.AlreadyLocked("held")

        monkeypatch.setattr(_liveness, "_verify_opened_regular", _verify)
        monkeypatch.setattr(_liveness.portalocker, "lock", _already_locked)

        state = _liveness.probe_legacy_pid_file(legacy)

        assert calls == 2
        assert state.alive is True
        assert state.pid is None
        assert state.legacy_lock_mode is None
        assert state.probe_error is not None

    @pytest.mark.skipif(os.name == "nt", reason="legacy shared flock is POSIX-only")
    def test_shared_legacy_holder_does_not_gate(self, rt: Path, tmp_path: Path):
        """#2003: a shared legacy holder is a modern server's compatibility
        alias — that server is gated by its own runtime pid file, so the
        alias must not block work on an unrelated store sharing the HOME."""
        from memtomem._runtime_paths import legacy_server_pid_path
        from memtomem.cli._liveness import check_server_liveness

        db = tmp_path / "store" / "memtomem.db"
        with self._hold_shared(legacy_server_pid_path()):
            scoped = check_server_liveness(db)
            agnostic = check_server_liveness()

        assert scoped.alive is False, (
            "a shared legacy alias must not gate the store-scoped probe (#2003)"
        )
        assert agnostic.alive is False, (
            "a shared legacy alias must not gate the store-agnostic probe (#2003)"
        )

    @pytest.mark.skipif(os.name == "nt", reason="legacy shared flock is POSIX-only")
    def test_exclusive_legacy_holder_still_gates(self, rt: Path, tmp_path: Path):
        from memtomem._runtime_paths import legacy_server_pid_path
        from memtomem.cli._liveness import check_server_liveness

        db = tmp_path / "store" / "memtomem.db"
        with self._hold(legacy_server_pid_path()):
            scoped = check_server_liveness(db)
            agnostic = check_server_liveness()

        assert scoped.alive is True and scoped.pid_file == legacy_server_pid_path(), (
            "an exclusive legacy holder (pre-0.1.25 server) must still refuse"
        )
        assert agnostic.alive is True and agnostic.pid_file == legacy_server_pid_path()

    @pytest.mark.skipif(os.name == "nt", reason="legacy shared flock is POSIX-only")
    def test_unclassifiable_legacy_probe_fails_closed(
        self, rt: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Pins the fail-closed guard against simplifying the shared-skip
        in ``_probe_legacy_gate`` to ``mode != "exclusive"`` — a probe that
        could not classify the lock has no evidence and must gate."""
        import memtomem.cli._liveness as liveness
        from memtomem._runtime_paths import legacy_server_pid_path

        broken = liveness.ServerState(
            alive=True,
            pid=None,
            pid_file=legacy_server_pid_path(),
            probe_error="legacy lock classification failed: boom",
        )
        monkeypatch.setattr(liveness, "probe_legacy_pid_file", lambda: broken)

        db = tmp_path / "store" / "memtomem.db"
        scoped = liveness.check_server_liveness(db)
        agnostic = liveness.check_server_liveness()

        assert scoped.alive is True and scoped.probe_error is not None
        assert agnostic.alive is True and agnostic.probe_error is not None

    def test_sees_server_in_a_transition_runtime_dir(
        self, rt: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A pre-#2037 server at a safe derivable root remains visible."""

        from memtomem._runtime_paths import server_pid_path
        from memtomem.cli._liveness import check_server_liveness

        sibling = tmp_path / "legacy-runtime"
        sibling.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            sibling.chmod(0o700)
        assert sibling != rt

        import memtomem.cli._liveness as liveness

        monkeypatch.setattr(liveness, "candidate_runtime_dirs", lambda: [rt, sibling])

        db = tmp_path / "store" / "memtomem.db"
        with self._hold(sibling / server_pid_path(db).name):
            scoped = check_server_liveness(db)
            agnostic = check_server_liveness()

        assert scoped.alive is True, (
            "a live server in a transition runtime dir must gate this store"
        )
        assert agnostic.alive is True

    def test_runtime_candidates_cover_stable_and_transition_roots(self, monkeypatch):
        """Pins the stable-first transition set against a silent narrowing."""
        import tempfile

        from memtomem._runtime_paths import _is_safe_dir, candidate_runtime_dirs, runtime_dir

        monkeypatch.delenv("_MEMTOMEM_TEST_RUNTIME_DIR")
        uid = os.geteuid() if hasattr(os, "geteuid") else 0
        dirs = candidate_runtime_dirs()

        assert dirs[0] == runtime_dir(), "the stable anchor must be probed first"
        assert Path(tempfile.gettempdir()) / f"memtomem-{uid}" in dirs
        if os.name != "nt":
            # Gated on the same safety check ``runtime_dir`` applies to
            # ``$XDG_RUNTIME_DIR``: a base that fails it is one no server
            # could have resolved to, and probing an unsearchable one would
            # fail closed and refuse every destructive command (#2003 review).
            systemd = Path(f"/run/user/{uid}")
            if _is_safe_dir(systemd):
                assert systemd / "memtomem" in dirs, (
                    "the systemd location must be probed for a server whose "
                    "XDG_RUNTIME_DIR this process cannot read"
                )
            else:
                assert systemd / "memtomem" not in dirs, (
                    "an unusable systemd base must not be probed — it fails "
                    "closed and blocks uninstall with no user remedy"
                )
        assert len(dirs) == len(set(dirs)), "candidates must be de-duplicated"

    def test_web_probe_sees_transition_runtime_root(
        self, rt: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        import memtomem.cli._liveness as liveness

        legacy = tmp_path / "legacy-runtime"
        legacy.mkdir(mode=0o700)
        if os.name != "nt":
            legacy.chmod(0o700)
        monkeypatch.setattr(liveness, "candidate_runtime_dirs", lambda: [rt, legacy])

        with self._hold(legacy / "web.pid"):
            state = liveness.check_web_liveness()

        assert state.alive is True
        assert state.pid_file == legacy / "web.pid"

    def test_unresolvable_pid_candidates_fail_closed(self, monkeypatch: pytest.MonkeyPatch):
        """A probe that cannot enumerate *where* a server might be has no
        evidence that none exists, so it must refuse rather than narrow the
        candidate set to the caller's own path (#2003 review)."""
        import memtomem.cli._liveness as liveness

        def _boom():
            raise OSError("no temp dir")

        monkeypatch.setattr(liveness, "candidate_runtime_dirs", _boom)

        scoped = liveness.check_server_liveness(Path("/tmp/store/memtomem.db"))
        agnostic = liveness.check_server_liveness()
        states, _warning = liveness.enumerate_server_liveness_inventory()

        assert scoped.alive is True and scoped.probe_error is not None
        assert agnostic.alive is True and agnostic.probe_error is not None
        assert any(s.probe_error is not None for s in states)

    @pytest.mark.requires_symlinks
    def test_symlinked_current_runtime_candidate_fails_closed(
        self, rt: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        import memtomem.cli._liveness as liveness

        target = tmp_path / "redirect-target"
        target.mkdir(mode=0o700)
        target.chmod(0o700)
        (target / "server.pid").write_text("987654\n", encoding="utf-8")
        redirected = tmp_path / "runtime-link"
        redirected.symlink_to(target, target_is_directory=True)
        monkeypatch.setattr(liveness, "runtime_dir", lambda: redirected)
        monkeypatch.setattr(liveness, "candidate_runtime_dirs", lambda: [redirected])

        state = liveness.check_server_liveness(Path("/tmp/store/memtomem.db"))

        assert state.alive is True
        assert state.pid is None
        assert state.probe_error is not None
        assert "symlink" in state.probe_error

    @pytest.mark.requires_symlinks
    def test_symlinked_speculative_runtime_candidate_is_skipped(
        self, rt: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        import memtomem.cli._liveness as liveness

        target = tmp_path / "redirect-target"
        target.mkdir(mode=0o700)
        target.chmod(0o700)
        redirected = tmp_path / "runtime-link"
        redirected.symlink_to(target, target_is_directory=True)
        monkeypatch.setattr(liveness, "candidate_runtime_dirs", lambda: [rt, redirected])

        state = liveness.check_server_liveness(Path("/tmp/store/memtomem.db"))

        assert state.alive is False
        assert state.probe_error is None
        assert state.probe_warning == f"skipped: {redirected} (symlink)"

    @pytest.mark.skipif(os.name == "nt", reason="POSIX runtime permission policy")
    def test_loose_speculative_runtime_candidate_is_skipped(
        self, rt: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        import memtomem.cli._liveness as liveness

        loose = tmp_path / "loose-\x1b-runtime"
        loose.mkdir(mode=0o755)
        loose.chmod(0o755)
        monkeypatch.setattr(liveness, "candidate_runtime_dirs", lambda: [rt, loose])

        files, detail = liveness._glob_server_pid_files()
        states, inventory_warning = liveness.enumerate_server_liveness_inventory()
        state = liveness.check_server_liveness(Path("/tmp/store/memtomem.db"))

        assert files == []
        assert states == []
        assert detail == f"skipped: {liveness.scrub_text(str(loose))} (unsafe permissions 0o755)"
        assert inventory_warning == detail
        assert "\x1b" not in detail
        assert "\\x1b" in detail
        assert state.alive is False
        assert state.probe_error is None
        assert state.probe_warning == detail

    def test_generic_skip_detail_is_bounded_without_splitting_scrub_escapes(self):
        import memtomem.cli._liveness as liveness

        candidate = Path("/tmp/speculative-runtime")
        detail = liveness._runtime_candidate_skip_detail(
            candidate,
            OSError("\x1b" * 31),
        )
        reason = detail.removeprefix(f"skipped: {candidate} (OSError: ").removesuffix(")")

        assert "\x1b" not in detail
        assert reason == "\\x1b" * 29 + "..."
        assert len(reason) <= liveness._SKIP_DETAIL_MAX_CHARS

    @pytest.mark.skipif(os.name == "nt", reason="POSIX runtime permission policy")
    def test_loose_current_runtime_candidate_fails_closed_and_scrubs_path(
        self, rt: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        import memtomem.cli._liveness as liveness

        loose = tmp_path / "loose-\x1b-runtime"
        loose.mkdir(mode=0o755)
        loose.chmod(0o755)
        monkeypatch.setattr(liveness, "runtime_dir", lambda: loose)
        monkeypatch.setattr(liveness, "candidate_runtime_dirs", lambda: [loose])

        state = liveness.check_server_liveness(Path("/tmp/store/memtomem.db"))

        assert state.alive is True
        assert state.pid is None
        assert state.probe_error is not None
        assert "PermissionError" in state.probe_error
        assert "unsafe permissions" in state.probe_error
        assert "\x1b" not in state.probe_error
        assert "\\x1b" in state.probe_error

    def test_store_agnostic_probe_fails_closed_on_non_directory_candidate(
        self, rt: Path, monkeypatch: pytest.MonkeyPatch
    ):
        import memtomem.cli._liveness as liveness

        candidate = rt / "not-a-directory"
        candidate.write_text("occupied", encoding="utf-8")
        monkeypatch.setattr(liveness, "runtime_dir", lambda: candidate)
        monkeypatch.setattr(liveness, "candidate_runtime_dirs", lambda: [candidate])

        files, detail = liveness._glob_server_pid_files()
        states, _warning = liveness.enumerate_server_liveness_inventory()
        state = liveness.check_server_liveness()

        assert files is None
        assert str(candidate) in detail
        assert len(states) == 1
        assert states[0].alive is True
        assert states[0].pid_file is None
        assert states[0].probe_error is not None
        assert str(candidate) in states[0].probe_error
        assert state.alive is True
        assert state.pid_file is None
        assert state.probe_error is not None
        assert str(candidate) in state.probe_error

    def test_store_pid_scan_surfaces_permission_error(
        self, rt: Path, monkeypatch: pytest.MonkeyPatch
    ):
        import memtomem.cli._liveness as liveness

        monkeypatch.setattr(liveness, "candidate_runtime_dirs", lambda: [rt])
        real_scandir = os.scandir

        def _deny_scan(path):
            if Path(path) != rt:
                return real_scandir(path)
            raise PermissionError("unsearchable runtime dir")

        monkeypatch.setattr(liveness.os, "scandir", _deny_scan)

        files, detail = liveness._glob_server_pid_files()

        assert files is None
        assert str(rt) in detail
        assert "unsearchable runtime dir" in detail

    @pytest.mark.skipif(
        os.name != "posix" or getattr(os, "geteuid", lambda: 0)() == 0,
        reason="requires permission bits enforced for a non-root POSIX user",
    )
    def test_store_pid_scan_fails_closed_on_real_permission_error(
        self, rt: Path, monkeypatch: pytest.MonkeyPatch
    ):
        import memtomem.cli._liveness as liveness

        pid_file = rt / "server-aaaaaaaaaaaaaaaa.pid"
        pid_file.write_text("4242", encoding="utf-8")
        monkeypatch.setattr(liveness, "candidate_runtime_dirs", lambda: [rt])

        os.chmod(rt, 0o000)
        try:
            files, detail = liveness._glob_server_pid_files()
            states, _warning = liveness.enumerate_server_liveness_inventory()
            state = liveness.check_server_liveness()
        finally:
            os.chmod(rt, 0o700)

        assert files is None
        assert str(rt) in detail
        assert len(states) == 1
        assert states[0].alive is True
        assert states[0].pid_file is None
        assert states[0].probe_error is not None
        assert state.alive is True
        assert state.pid_file is None
        assert state.probe_error is not None
        assert pid_file.exists(), "the hidden pid file must not be mistaken for an absent file"

    def test_store_pid_scan_fails_closed_during_iteration(
        self, rt: Path, monkeypatch: pytest.MonkeyPatch
    ):
        import memtomem.cli._liveness as liveness

        class _Entry:
            name = "server-aaaaaaaaaaaaaaaa.pid"

        real_scandir = os.scandir

        @contextlib.contextmanager
        def _broken_scan(path):
            if Path(path) != rt:
                with real_scandir(path) as entries:
                    yield entries
                return

            def _entries():
                yield _Entry()
                raise PermissionError("runtime dir changed during scan")

            yield _entries()

        monkeypatch.setattr(liveness, "candidate_runtime_dirs", lambda: [rt])
        monkeypatch.setattr(liveness.os, "scandir", _broken_scan)

        files, detail = liveness._glob_server_pid_files()

        assert files is None
        assert str(rt) in detail
        assert "runtime dir changed during scan" in detail

    def test_missing_runtime_candidate_is_empty(self, rt: Path, monkeypatch: pytest.MonkeyPatch):
        import memtomem.cli._liveness as liveness

        missing = rt / "missing"
        monkeypatch.setattr(liveness, "candidate_runtime_dirs", lambda: [missing])

        files, detail = liveness._glob_server_pid_files()
        states, warning = liveness.enumerate_server_liveness_inventory()
        state = liveness.check_server_liveness()

        assert files == []
        assert detail == ""
        assert states == []
        assert state.alive is False
        assert state.pid_file is None
        assert state.probe_error is None
        assert warning is None
        assert not missing.exists(), "read-only liveness probes must not create runtime dirs"
