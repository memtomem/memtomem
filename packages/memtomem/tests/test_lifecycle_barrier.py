"""Lifecycle barrier (#1936): the uninstall ↔ server-startup TOCTOU gate.

``mm uninstall`` used to snapshot liveness and then stage state with
nothing held in between, so a server opening the store in that window had
its live database staged out from under it. The barrier closes that: the
server takes it *shared* before storage opens and holds it for the
process lifetime, uninstall takes it *exclusive* across its final
re-probe and the whole staging phase.

Contention is validated **cross-process** (spawn) per the repo convention
(``test_locking_contention.py``): flock/``LockFileEx`` are process-level,
and Windows may even grant a second same-process handle, so in-process
contention proves nothing.

One rule shapes every release test here: the autouse
``_isolated_instance_registry`` fixture sweeps leaked barriers at
teardown, so "a later test passed" is never evidence that production code
released anything. Release paths are proven by **re-acquiring inside the
same test**.
"""

from __future__ import annotations

import builtins
import errno
import multiprocessing as mp
import os
import time
from pathlib import Path

import portalocker
import pytest

import memtomem._instance_registry as reg

_CTX = mp.get_context("spawn")


# ----------------------------------------------------------------- helpers


@pytest.fixture
def rt(tmp_path, monkeypatch) -> Path:
    """A barrier-of-record for one test, overriding the conftest default so
    spawned children (which see no fixtures) can be pointed at the same
    directory by path string."""
    target = tmp_path / "rt"

    def _rt() -> Path:
        return target

    def _ensure() -> Path:
        target.mkdir(mode=0o700, exist_ok=True)
        return target

    monkeypatch.setattr(reg, "runtime_dir", _rt)
    monkeypatch.setattr(reg, "ensure_runtime_dir", _ensure)
    return target


def _drain_until(q, tag: str, timeout: float = 30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            msg = q.get(timeout=1.0)
        except Exception:
            continue
        if msg[0] == tag:
            return msg
    raise AssertionError(f"child never reported {tag!r}")


def _stop(proc: mp.Process) -> None:
    if proc.is_alive():
        proc.kill()
    proc.join(timeout=30)


# ------------------------------------------------------- spawn child bodies


def _child_setup(rt_str: str):
    import memtomem._instance_registry as _reg

    target = Path(rt_str)

    def _rt() -> Path:
        return target

    def _ensure() -> Path:
        target.mkdir(mode=0o700, exist_ok=True)
        return target

    _reg.runtime_dir = _rt
    _reg.ensure_runtime_dir = _ensure
    return _reg


def _child_hold_shared(rt_str: str, q, release) -> None:
    _reg = _child_setup(rt_str)
    barrier = _reg.acquire_server_lifecycle_barrier()
    q.put(("held", os.getpid()))
    release.wait(60)
    barrier.release()
    q.put(("released",))


def _child_hold_exclusive(rt_str: str, q, release) -> None:
    _reg = _child_setup(rt_str)
    barrier = _reg.acquire_uninstall_lifecycle_barrier()
    q.put(("held", os.getpid()))
    release.wait(60)
    barrier.release()
    q.put(("released",))


def _child_hold_exclusive_forever(rt_str: str, q) -> None:
    _reg = _child_setup(rt_str)
    _reg.acquire_uninstall_lifecycle_barrier()
    q.put(("held", os.getpid()))
    time.sleep(600)  # parent kills us


def _child_hold_fork_grandchild(rt_str: str, q, release) -> None:
    import sys

    _reg = _child_setup(rt_str)
    barrier = _reg.acquire_server_lifecycle_barrier()
    grand = os.fork()
    if grand == 0:
        # The inherited handle must survive this: release() pid-guards,
        # then normal interpreter exit closes only the child's copy of the
        # descriptor, which does not drop the parent's flock.
        barrier.release()
        sys.exit(0)
    _, status = os.waitpid(grand, 0)
    q.put(("forked", not barrier._closed, os.waitstatus_to_exitcode(status)))
    release.wait(60)
    barrier.release()


# ------------------------------------------------- cross-process contention


class TestBarrierContention:
    """The reader/writer semantics the whole design rests on."""

    def test_shared_holder_refuses_exclusive(self, rt):
        """A live server (shared) must block uninstall (exclusive)."""
        q, release = _CTX.Queue(), _CTX.Event()
        holder = _CTX.Process(target=_child_hold_shared, args=(str(rt), q, release))
        holder.start()
        try:
            _drain_until(q, "held")
            with pytest.raises(reg.BarrierTimeout):
                reg.acquire_uninstall_lifecycle_barrier(timeout_s=0.3)
        finally:
            release.set()
            holder.join(timeout=30)
            _stop(holder)

    def test_exclusive_holder_refuses_shared(self, rt):
        """An in-flight uninstall must block a starting server."""
        q, release = _CTX.Queue(), _CTX.Event()
        holder = _CTX.Process(target=_child_hold_exclusive, args=(str(rt), q, release))
        holder.start()
        try:
            _drain_until(q, "held")
            with pytest.raises(reg.BarrierTimeout):
                reg.acquire_server_lifecycle_barrier(timeout_s=0.3)
        finally:
            release.set()
            holder.join(timeout=30)
            _stop(holder)

    def test_two_shared_holders_coexist(self, rt):
        """Two servers must not block each other — only uninstall is
        exclusive. If shared ever collapsed to exclusive (the portalocker
        Windows msvcrt fallback would do exactly that), a second server
        could never initialize."""
        q, release = _CTX.Queue(), _CTX.Event()
        holder = _CTX.Process(target=_child_hold_shared, args=(str(rt), q, release))
        holder.start()
        try:
            _drain_until(q, "held")
            mine = reg.acquire_server_lifecycle_barrier(timeout_s=5.0)
            mine.release()
        finally:
            release.set()
            holder.join(timeout=30)
            _stop(holder)

    def test_released_barrier_is_reacquirable_cross_process(self, rt):
        """Release actually drops the flock — not merely the bookkeeping."""
        q, release = _CTX.Queue(), _CTX.Event()
        holder = _CTX.Process(target=_child_hold_exclusive, args=(str(rt), q, release))
        holder.start()
        try:
            _drain_until(q, "held")
            release.set()
            _drain_until(q, "released")
            reg.acquire_server_lifecycle_barrier(timeout_s=5.0).release()
        finally:
            release.set()
            holder.join(timeout=30)
            _stop(holder)

    def test_killed_holder_frees_the_barrier(self, rt):
        """A held flock is never stale — the kernel releases it on death.

        This is what makes uninstall's refusal non-``--force``-overridable:
        there is no such thing as a leftover barrier needing an override.
        """
        q = _CTX.Queue()
        holder = _CTX.Process(target=_child_hold_exclusive_forever, args=(str(rt), q))
        holder.start()
        try:
            _drain_until(q, "held")
            with pytest.raises(reg.BarrierTimeout):
                reg.acquire_server_lifecycle_barrier(timeout_s=0.3)
            holder.kill()
            holder.join(timeout=30)
            reg.acquire_server_lifecycle_barrier(timeout_s=5.0).release()
        finally:
            _stop(holder)


# --------------------------------------------------------- acquire polarity


class TestAcquireFailsClosed:
    def test_unusable_barrier_path_raises_for_both_surfaces(self, rt, monkeypatch):
        """Infrastructure failure fails closed too, not just contention:
        a barrier that cannot be opened cannot prove the absence of a
        destructive operation."""
        rt.mkdir(mode=0o700, exist_ok=True)
        # A directory where the lock file belongs — ``open`` raises.
        (rt / "lifecycle.lock").mkdir()
        with pytest.raises(OSError):
            reg.acquire_server_lifecycle_barrier(timeout_s=0.3)
        with pytest.raises(OSError):
            reg.acquire_uninstall_lifecycle_barrier(timeout_s=0.3)

    def test_open_failure_propagates_unwrapped(self, rt, monkeypatch):
        """The actionable error survives: a ``PermissionError`` naming the
        unusable runtime dir must reach the caller, not be flattened into
        a generic timeout."""
        boom = PermissionError(13, "runtime dir is not writable")
        monkeypatch.setattr(reg, "ensure_runtime_dir", lambda: (_ for _ in ()).throw(boom))
        with pytest.raises(PermissionError) as excinfo:
            reg.acquire_server_lifecycle_barrier(timeout_s=0.3)
        assert excinfo.value is boom

    def test_timeout_is_bounded_by_the_requested_budget(self, rt):
        """The wait is bounded — uninstall must refuse, never hang."""
        q, release = _CTX.Queue(), _CTX.Event()
        holder = _CTX.Process(target=_child_hold_shared, args=(str(rt), q, release))
        holder.start()
        try:
            _drain_until(q, "held")
            started = time.monotonic()
            with pytest.raises(reg.BarrierTimeout):
                reg.acquire_uninstall_lifecycle_barrier(timeout_s=0.3)
            assert time.monotonic() - started < 10.0
        finally:
            release.set()
            holder.join(timeout=30)
            _stop(holder)

    def test_module_knob_is_resolved_at_call_time(self, rt, monkeypatch):
        """``timeout_s=None`` must read the module constant when called.

        A ``timeout_s=_BARRIER_TIMEOUT_S`` default would bind at import and
        silently ignore any later tuning.
        """
        q, release = _CTX.Queue(), _CTX.Event()
        holder = _CTX.Process(target=_child_hold_exclusive, args=(str(rt), q, release))
        holder.start()
        try:
            _drain_until(q, "held")
            monkeypatch.setattr(reg, "_BARRIER_TIMEOUT_S", 0.2)
            started = time.monotonic()
            with pytest.raises(reg.BarrierTimeout):
                reg.acquire_server_lifecycle_barrier()
            assert time.monotonic() - started < 1.5, "patched knob was ignored"
        finally:
            release.set()
            holder.join(timeout=30)
            _stop(holder)


# ----------------------------------------------------- poll-loop classifier


class _FakePywinError(Exception):
    """Stand-in for ``pywintypes.error``: derives from ``Exception`` (not
    ``OSError``), carries ``.winerror`` and *no* ``.errno`` — exactly the
    real Win32 exception's shape (#1957) — so the classifier can be
    exercised on non-Windows CI. Defaults to the lock-violation code."""

    def __init__(
        self, winerror: int = reg._WINERROR_LOCK_VIOLATION, strerror: str = "lock violation"
    ) -> None:
        super().__init__(winerror, "LockFileEx", strerror)
        self.winerror = winerror
        self.strerror = strerror


class TestPollLoopClassifier:
    """#1957: the poll loop separates "someone holds the flock" from "the
    lock call itself failed". Per-guard pins — each classifier branch has
    its own test; a sibling branch passing is not evidence
    (``feedback_pin_test_mutation_validation``)."""

    @staticmethod
    def _lockexc(cause: BaseException | None) -> portalocker.LockException:
        exc = portalocker.LockException("lock call failed")
        exc.__cause__ = cause
        return exc

    @staticmethod
    def _patch_lock(monkeypatch, exc: BaseException) -> None:
        def _lock(fp, flags):
            raise exc

        monkeypatch.setattr(reg.portalocker, "lock", _lock)

    # -- non-contention I/O failures escape as OSError (fast, no polling) --

    @pytest.mark.parametrize(
        "acquire_name",
        ["acquire_uninstall_lifecycle_barrier", "acquire_server_lifecycle_barrier"],
    )
    def test_lockexception_eio_cause_escapes_as_oserror_fast(self, rt, monkeypatch, acquire_name):
        """A bare ``LockException`` chaining an ``EIO`` ``OSError`` is
        infrastructure, not contention: it escapes as ``OSError`` at once,
        preserving errno and naming the barrier path, for both surfaces."""
        cause = OSError(errno.EIO, "disk I/O error")
        exc = self._lockexc(cause)
        self._patch_lock(monkeypatch, exc)
        acquire = getattr(reg, acquire_name)
        started = time.monotonic()
        with pytest.raises(OSError) as excinfo:
            acquire(timeout_s=5.0)
        assert time.monotonic() - started < 2.0, "I/O failure must not wait out the deadline"
        assert excinfo.value.errno == errno.EIO
        assert "disk I/O error" in str(excinfo.value)
        assert str(reg.lifecycle_barrier_path()) in str(excinfo.value)
        assert excinfo.value.__cause__ is exc

    def test_lockexception_eoferror_cause_escapes_as_oserror(self, rt, monkeypatch):
        """The NFS shape (``LockException`` chaining ``EOFError``, no errno)
        still escapes as ``OSError`` — via the generic wrap that names the
        barrier path — rather than being mislabeled contention."""
        exc = self._lockexc(EOFError("nfs lockd down"))
        self._patch_lock(monkeypatch, exc)
        with pytest.raises(OSError) as excinfo:
            reg.acquire_uninstall_lifecycle_barrier(timeout_s=5.0)
        assert "lifecycle barrier lock failed" in str(excinfo.value)
        assert str(reg.lifecycle_barrier_path()) in str(excinfo.value)
        assert excinfo.value.__cause__ is exc

    def test_naked_oserror_escapes_identically(self, rt, monkeypatch):
        """A raw non-contention ``OSError`` is re-raised as-is (subtype and
        identity preserved), only backfilling the barrier path as its
        filename so the CLI has one to report."""
        boom = OSError(errno.EIO, "boom")
        self._patch_lock(monkeypatch, boom)
        with pytest.raises(OSError) as excinfo:
            reg.acquire_uninstall_lifecycle_barrier(timeout_s=5.0)
        assert excinfo.value is boom
        assert boom.filename == str(reg.lifecycle_barrier_path())

    def test_lock_io_failure_closes_the_descriptor(self, rt, monkeypatch):
        """The ownership block still runs on the new escape path: the
        barrier handle is closed (no leak) and the file is never unlinked
        (retained infrastructure)."""
        captured: dict[str, object] = {}
        real_open = builtins.open

        def spy_open(file, *args, **kwargs):
            fp = real_open(file, *args, **kwargs)
            if Path(str(file)).name == "lifecycle.lock":
                captured["fp"] = fp
            return fp

        monkeypatch.setattr(builtins, "open", spy_open)
        self._patch_lock(monkeypatch, self._lockexc(OSError(errno.EIO, "disk I/O error")))
        with pytest.raises(OSError):
            reg.acquire_uninstall_lifecycle_barrier(timeout_s=5.0)
        assert captured["fp"].closed is True
        assert reg.lifecycle_barrier_path().exists()

    # ------ genuine contention keeps polling to the BarrierTimeout ------

    def test_lockexception_eagain_cause_is_contention(self, rt, monkeypatch):
        exc = self._lockexc(OSError(errno.EAGAIN, "resource temporarily unavailable"))
        self._patch_lock(monkeypatch, exc)
        with pytest.raises(reg.BarrierTimeout):
            reg.acquire_uninstall_lifecycle_barrier(timeout_s=0.2)

    def test_lockexception_eacces_cause_is_contention(self, rt, monkeypatch):
        exc = self._lockexc(OSError(errno.EACCES, "permission denied"))
        self._patch_lock(monkeypatch, exc)
        with pytest.raises(reg.BarrierTimeout):
            reg.acquire_uninstall_lifecycle_barrier(timeout_s=0.2)

    def test_already_locked_without_cause_is_contention(self, rt, monkeypatch):
        """The ``isinstance`` branch stands alone — across portalocker
        3.0/3.1/3.2 a held lock is always ``AlreadyLocked``, cause or not
        (guards the #1944 type-drift note)."""
        self._patch_lock(monkeypatch, portalocker.AlreadyLocked("busy"))
        with pytest.raises(reg.BarrierTimeout):
            reg.acquire_uninstall_lifecycle_barrier(timeout_s=0.2)

    def test_blockingioerror_is_contention(self, rt, monkeypatch):
        """POSIX's ``BlockingIOError`` (an ``OSError`` subclass, often with
        no errno set) must not fall through to the OSError-escape branch."""
        self._patch_lock(monkeypatch, BlockingIOError())
        with pytest.raises(reg.BarrierTimeout):
            reg.acquire_uninstall_lifecycle_barrier(timeout_s=0.2)

    def test_winerror_lock_violation_cause_is_contention(self, rt, monkeypatch):
        """Windows contention chains a ``pywintypes.error`` carrying
        ``.winerror`` and no ``.errno``; the winerror probe catches it."""
        self._patch_lock(monkeypatch, self._lockexc(_FakePywinError()))
        with pytest.raises(reg.BarrierTimeout):
            reg.acquire_uninstall_lifecycle_barrier(timeout_s=0.2)

    def test_naked_permissionerror_eacces_is_contention(self, rt, monkeypatch):
        """The direct-errno superset: a raw ``EACCES`` ``OSError`` leaking
        past portalocker is fcntl-documented contention, not a path to
        repair."""
        self._patch_lock(monkeypatch, OSError(errno.EACCES, "permission denied"))
        with pytest.raises(reg.BarrierTimeout):
            reg.acquire_uninstall_lifecycle_barrier(timeout_s=0.2)

    # ---- raw pywintypes.error: classifier verdict (platform-independent) ----

    def test_raw_winerror_lock_violation_is_contention(self):
        """A *raw* ``pywintypes.error`` (not chained) with the lock-violation
        code reads as contention off the exception itself — the Win32 backend
        maps 33 to ``AlreadyLocked``, but a leaked raw 33 must not be sent to
        repair-the-path. Pure-function pin, so it runs on POSIX CI."""
        assert reg._is_lock_contention(_FakePywinError(reg._WINERROR_LOCK_VIOLATION)) is True

    def test_raw_non_violation_winerror_is_not_contention(self):
        """A raw non-33 ``pywintypes.error`` (portalocker re-raises these
        unwrapped) is a lock-call failure, not contention: the classifier
        rejects it and the normalizer turns it into an ``OSError`` naming the
        barrier path. Pure-function pin, so it runs on POSIX CI."""
        boom = _FakePywinError(32, "sharing violation")
        assert reg._is_lock_contention(boom) is False
        path = reg.lifecycle_barrier_path()
        with pytest.raises(OSError) as excinfo:
            reg._raise_lock_io_failure(boom, path)
        assert str(path) in str(excinfo.value)
        assert excinfo.value.__cause__ is boom

    def test_raw_pywintypes_error_escapes_the_poll_loop_as_oserror(self, rt, monkeypatch):
        """End-to-end through the real poll loop: a raw, non-``OSError``
        ``pywintypes.error`` must be *caught* (not escape unhandled) and
        normalized to ``OSError`` so the CLIs' repair branch fires (#1957).

        Simulates the Windows-only catch tuple on POSIX by injecting the fake
        Win32 type into ``_BARRIER_LOCK_ERRORS`` — on real Windows that tuple
        already carries ``pywintypes.error``.
        """
        monkeypatch.setattr(reg, "_BARRIER_LOCK_ERRORS", (*reg._LOCK_CONTENDED, _FakePywinError))
        self._patch_lock(monkeypatch, _FakePywinError(32, "sharing violation"))
        started = time.monotonic()
        with pytest.raises(OSError) as excinfo:
            reg.acquire_uninstall_lifecycle_barrier(timeout_s=5.0)
        assert time.monotonic() - started < 2.0, "a lock-call failure must not poll"
        assert not isinstance(excinfo.value, reg.BarrierTimeout)
        assert str(reg.lifecycle_barrier_path()) in str(excinfo.value)

    def test_raw_pywintypes_lock_violation_polls_to_barrier_timeout(self, rt, monkeypatch):
        """The contention twin of the escape test: a raw lock-violation Win32
        error keeps polling and refuses as ``BarrierTimeout``, not repair."""
        monkeypatch.setattr(reg, "_BARRIER_LOCK_ERRORS", (*reg._LOCK_CONTENDED, _FakePywinError))
        self._patch_lock(monkeypatch, _FakePywinError(reg._WINERROR_LOCK_VIOLATION))
        with pytest.raises(reg.BarrierTimeout):
            reg.acquire_uninstall_lifecycle_barrier(timeout_s=0.2)


# ------------------------------------------------------------ held handle


class TestHeldBarrier:
    def test_release_is_idempotent(self, rt):
        barrier = reg.acquire_server_lifecycle_barrier(timeout_s=5.0)
        barrier.release()
        barrier.release()  # must not raise

    def test_handle_is_hashable(self, rt):
        """``eq=False`` keeps identity hashing. A value-comparing dataclass
        is unhashable, and the ``_active_barriers`` insert would raise
        *after* the flock was taken — leaking a hold nothing can release.
        """
        first = reg.acquire_server_lifecycle_barrier(timeout_s=5.0)
        second = reg.acquire_server_lifecycle_barrier(timeout_s=5.0)
        try:
            assert first != second, "distinct holds must not compare equal"
            assert len({first, second}) == 2
        finally:
            first.release()
            second.release()

    def test_release_removes_it_from_the_active_set(self, rt):
        barrier = reg.acquire_server_lifecycle_barrier(timeout_s=5.0)
        assert barrier in reg._active_barriers
        barrier.release()
        assert barrier not in reg._active_barriers

    def test_barrier_file_is_never_unlinked(self, rt):
        """Retained infrastructure: unlinking a lock file lets a blocked
        waiter acquire the orphaned inode while newcomers lock a fresh
        one."""
        barrier = reg.acquire_server_lifecycle_barrier(timeout_s=5.0)
        path = reg.lifecycle_barrier_path()
        assert path.exists()
        barrier.release()
        assert path.exists()


@pytest.mark.skipif(os.name == "nt", reason="fork is POSIX-only")
class TestForkContract:
    def test_forked_child_cannot_release_the_parents_barrier(self, rt):
        """The pid guard mirrors ``RegisteredInstance.cleanup``: a child
        that inherited the descriptor must not drop a hold the parent
        still relies on.

        Forking happens inside a *spawned* worker, not in the pytest
        process — mirrors ``test_instance_registry.py``'s fork test and
        avoids forking an interpreter full of threads.
        """
        q, release = _CTX.Queue(), _CTX.Event()
        worker = _CTX.Process(target=_child_hold_fork_grandchild, args=(str(rt), q, release))
        worker.start()
        try:
            _, still_held, grand_code = _drain_until(q, "forked")
            assert grand_code == 0
            assert still_held, "the child's release() must not close the parent's handle"
            # Cross-process truth: the worker's shared hold survives, so an
            # exclusive acquire from here still refuses.
            with pytest.raises(reg.BarrierTimeout):
                reg.acquire_uninstall_lifecycle_barrier(timeout_s=0.3)
        finally:
            release.set()
            worker.join(timeout=30)
            _stop(worker)
