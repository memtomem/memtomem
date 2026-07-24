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

import multiprocessing as mp
import os
import time
from pathlib import Path

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
