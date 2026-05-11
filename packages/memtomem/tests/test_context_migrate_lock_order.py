"""ADR-0011 PR-E4 — deterministic lock-acquire order regression test.

Pins the contract that :func:`memtomem.context.migrate._acquire_pair_lock`
takes the two sidecar locks in ``sorted(key=str)`` order regardless of
which path the caller passes as ``path_a`` vs ``path_b``. Without this
ordering, two concurrent inverse migrations
(``A: foo user→project_shared`` and ``B: foo project_shared→user``)
would each grab their src-side lock first and deadlock waiting for the
other's dst-side lock.

Two pins:

1. **Order pin** — ``_acquire_pair_lock(a, b)`` and ``_acquire_pair_lock(b, a)``
   acquire locks in the same global sequence (verified via a tracker
   that watches the order locks are obtained and released).
2. **Deadlock-freedom pin** — two threads racing inverse migrations
   complete within a generous timeout (no deadlock).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from memtomem.context._atomic import _file_lock, _lock_path_for
from memtomem.context.migrate import _acquire_pair_lock


def _acquire_log(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Wrap ``_file_lock`` so each acquire appends its lock_path to a shared list.

    The wrapper installs at the module path ``_acquire_pair_lock`` reads
    from (``memtomem.context.migrate._file_lock``). The original
    ``_file_lock`` implementation is delegated to verbatim — we only
    record the call sequence.
    """
    log: list[Path] = []
    real = _file_lock

    def wrapped(lock_path: Path):
        log.append(lock_path)
        return real(lock_path)

    monkeypatch.setattr("memtomem.context.migrate._file_lock", wrapped)
    return log


def test_acquire_pair_lock_orders_by_str(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Order pin — both call orientations produce the same global lock sequence."""
    a = tmp_path / "alpha"
    b = tmp_path / "beta"
    a.mkdir()
    b.mkdir()

    expected_first = min(_lock_path_for(a), _lock_path_for(b), key=str)
    expected_second = max(_lock_path_for(a), _lock_path_for(b), key=str)

    log_ab = _acquire_log(monkeypatch)
    with _acquire_pair_lock(a, b):
        pass
    assert log_ab == [expected_first, expected_second], (
        f"a→b orientation: got {log_ab}, expected [{expected_first}, {expected_second}]"
    )

    log_ba = _acquire_log(monkeypatch)
    with _acquire_pair_lock(b, a):
        pass
    assert log_ba == [expected_first, expected_second], (
        f"b→a orientation: got {log_ba}, expected [{expected_first}, {expected_second}]"
    )


def test_acquire_pair_lock_same_path_acquires_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Defensive: when both args resolve to the same lock, only one acquire."""
    p = tmp_path / "same"
    p.mkdir()
    log = _acquire_log(monkeypatch)
    with _acquire_pair_lock(p, p):
        pass
    assert log == [_lock_path_for(p)]


def test_inverse_migrate_no_deadlock(tmp_path: Path):
    """Deadlock-freedom pin — two threads racing inverse pair-locks both finish.

    Sets up two paths ``alpha`` and ``beta``; thread A locks
    ``(alpha, beta)`` while thread B locks ``(beta, alpha)``. Without
    sorted ordering, thread A would wait on beta's lock while thread B
    waits on alpha's, deadlocking. Sorted ordering forces both threads
    to acquire alpha's lock first, so one finishes its critical section
    and releases before the other proceeds.

    We verify both threads complete inside a generous timeout. The hold
    duration inside the lock is short (a 50 ms sleep) so the test
    finishes quickly when no deadlock; under deadlock it would hit the
    join timeout.
    """
    a = tmp_path / "alpha"
    b = tmp_path / "beta"
    a.mkdir()
    b.mkdir()

    barrier = threading.Barrier(2)
    finished: dict[str, bool] = {"A": False, "B": False}

    def thread_a():
        barrier.wait(timeout=5)
        with _acquire_pair_lock(a, b):
            time.sleep(0.05)
        finished["A"] = True

    def thread_b():
        barrier.wait(timeout=5)
        with _acquire_pair_lock(b, a):
            time.sleep(0.05)
        finished["B"] = True

    ta = threading.Thread(target=thread_a)
    tb = threading.Thread(target=thread_b)
    ta.start()
    tb.start()
    ta.join(timeout=5)
    tb.join(timeout=5)

    assert not ta.is_alive(), "thread A still alive — possible deadlock"
    assert not tb.is_alive(), "thread B still alive — possible deadlock"
    assert finished["A"] and finished["B"]
