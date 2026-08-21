"""Measured Windows lock semantics for the registry and the barrier (#2102).

Every other lock test in this repo validates contention **cross-process**
(spawn), because that is the contract the locks exist for and because
in-process behavior rides on backend details — see
``test_locking_contention.py`` and ``test_instance_registry.py``. This module
is the deliberate exception: the *question it answers is* what the Windows
backend does with a second handle opened by the same process, so it asserts
in-process outcomes on purpose. Nothing here should be read as a substitute
for the spawn convention, and nothing here should be copied into a test of a
cross-process guarantee.

Why it is pinned rather than left to prose: several comments in
``_instance_registry.py`` used to justify live code with the claim that
Windows *grants* a same-process second handle the same exclusive lock. #2101
found that claim unsupported (its only cited evidence, #759, shows the
opposite — a *blocking* acquire raising instead of waiting) and rewrote those
comments to be backend-neutral. #2102 measured the real behavior on
``windows-latest``; these tests are that measurement, kept executable so a
portalocker bump cannot silently invalidate the comments that cite it.

The two questions, and where the answers are consumed:

1. Does a second same-process handle contend for ``LOCK_EX | LOCK_NB``?
   If it does, ``enumerate_live_instances``' skip of its own registrations
   (``_enumerate_live_instances_at``) is an optimization — a self-probe would
   answer ``live``, which is the correct answer anyway. If it were granted,
   the skip would be load-bearing correctness.
2. Do the two Windows backends the lifecycle barrier mixes contend with each
   other? The server side takes ``LOCK_SH`` (delegated to ``Win32Locker`` /
   ``LockFileEx``) while uninstall/reset take ``LOCK_EX`` (``MsvcrtLocker`` /
   ``msvcrt.locking``) — different APIs over overlapping byte ranges.

What was measured (``windows-latest``, one throwaway matrix run over the
three portalocker versions this repo's ``>=3.0`` floor can resolve — 3.1.1,
3.2.0, 4.1.0): **every** version contends. A second same-process handle asking
for ``LOCK_EX | LOCK_NB`` is refused with ``AlreadyLocked``, so a self-probe
answers ``live``; the registry's self-skip is an optimization, not correctness.
The two backends also contend with each other in both directions. Only the
*shape* of the refusal moves with the version: 3.2.0+ refuse the exclusive lock
through ``msvcrt.locking`` (``OSError``, errno 13), while 3.0/3.1 route
exclusive locks through ``LockFileEx`` too (``pywintypes.error``, winerror 33 —
not an ``OSError``). The helpers below classify rather than pin that shape,
which is exactly the drift a bare comment could not have caught.

Handles are opened ``"a+b"`` throughout, matching ``_mutation_lock``:
``msvcrt.locking`` rejects a read-only handle and ``"w"`` would truncate.
"""

from __future__ import annotations

import contextlib
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import IO

import portalocker
import pytest

import memtomem._instance_registry as reg

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="measures the Windows lock backends (msvcrt.locking / LockFileEx)",
)

# ``MsvcrtLocker`` maps exactly these errnos to ``AlreadyLocked``; which one a
# given Windows build reports for a refused ``LK_NBLCK`` is not contractual,
# so the tests pin the classification (contention) and report the observed
# value rather than pinning the number.
_MSVCRT_CONTENTION_ERRNOS = (13, 16, 33, 36)
_ERROR_LOCK_VIOLATION = 33


@pytest.fixture
def rt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A registry-of-record for one test (same shape as the fixture in
    ``test_instance_registry.py``; no spawned children need it here)."""
    target = tmp_path / "rt"

    def _rt() -> Path:
        return target

    def _ensure() -> Path:
        target.mkdir(mode=0o700, exist_ok=True)
        return target

    monkeypatch.setattr(reg, "runtime_dir", _rt)
    monkeypatch.setattr(reg, "ensure_runtime_dir", _ensure)
    return target


@pytest.fixture
def db(tmp_path: Path) -> Path:
    p = tmp_path / "store.db"
    p.write_bytes(b"sqlite-fake")
    return p


@pytest.fixture
def lock_path(tmp_path: Path) -> Path:
    p = tmp_path / "measured.lock"
    p.touch()
    return p


@contextlib.contextmanager
def _handle(path: Path) -> Iterator[IO[bytes]]:
    fp = open(path, "a+b")
    try:
        yield fp
    finally:
        fp.close()


def _assert_exclusive_contention(exc: portalocker.AlreadyLocked) -> None:
    """The exclusive path refused, and refused *for contention*.

    Which backend refused is version-dependent and deliberately not pinned:
    ``portalocker>=3.0`` is the declared floor, and 3.0/3.1 route exclusive
    locks through ``LockFileEx`` (a ``pywintypes.error``, which is **not** an
    ``OSError``) while 3.2.0 onwards use ``msvcrt.locking`` (an ``OSError``
    from the contention errno set). Measured on both — see the module
    docstring. What is pinned is the classification: contention, either way.
    """
    cause = exc.__cause__
    if isinstance(cause, OSError):
        assert cause.errno in _MSVCRT_CONTENTION_ERRNOS, f"unexpected errno {cause.errno}"
        print(f"[#2102] msvcrt.locking refused with errno={cause.errno}")
        return
    winerror = getattr(cause, "winerror", None)
    assert winerror == _ERROR_LOCK_VIOLATION, f"unexpected refusal cause {cause!r}"
    print(f"[#2102] LockFileEx refused the exclusive lock with winerror={winerror}")


def _assert_win32_contention(exc: portalocker.AlreadyLocked) -> None:
    """The shared path refused with ``ERROR_LOCK_VIOLATION``."""
    cause = exc.__cause__
    winerror = getattr(cause, "winerror", None)
    assert winerror == _ERROR_LOCK_VIOLATION, f"unexpected winerror {winerror!r} ({cause!r})"
    print(f"[#2102] LockFileEx refused with winerror={winerror}")


class TestSameProcessExclusive:
    """Question 1 — a second handle from *this* process."""

    def test_second_handle_exclusive_nb_contends(self, lock_path: Path) -> None:
        """The raw backend: two handles, one file, exclusive both times."""
        with _handle(lock_path) as holder, _handle(lock_path) as contender:
            portalocker.lock(holder, portalocker.LOCK_EX)
            try:
                with pytest.raises(portalocker.AlreadyLocked) as excinfo:
                    portalocker.lock(contender, portalocker.LOCK_EX | portalocker.LOCK_NB)
                _assert_exclusive_contention(excinfo.value)
            finally:
                portalocker.unlock(holder)

    def test_self_probe_reports_live(self, rt: Path, db: Path) -> None:
        """The registry-level consequence: probing our own sentinel — the
        call ``_enumerate_live_instances_at`` skips — answers ``live``, so
        the skip is an optimization, not correctness."""
        inst = reg.register_instance(db)
        assert inst is not None
        try:
            assert reg._probe_entry(inst.path) == "live"
        finally:
            inst.cleanup()

    def test_failed_probe_does_not_release_the_holder(self, lock_path: Path) -> None:
        """``_probe_entry`` closes its handle without unlocking on ``live``.
        A refused attempt plus that close must leave the holder's lock
        standing — otherwise probing would strip the lock it just detected."""
        with _handle(lock_path) as holder:
            portalocker.lock(holder, portalocker.LOCK_EX)
            try:
                with _handle(lock_path) as probe:
                    with pytest.raises(portalocker.AlreadyLocked):
                        portalocker.lock(probe, portalocker.LOCK_EX | portalocker.LOCK_NB)
                with _handle(lock_path) as after:
                    with pytest.raises(portalocker.AlreadyLocked):
                        portalocker.lock(after, portalocker.LOCK_EX | portalocker.LOCK_NB)
            finally:
                portalocker.unlock(holder)


class TestMixedBackendBarrier:
    """Question 2 — the lifecycle barrier's ``LOCK_SH``/``LOCK_EX`` split
    crosses two Windows APIs (``LockFileEx`` and ``msvcrt.locking``)."""

    def test_exclusive_contends_with_shared_holder(self, lock_path: Path) -> None:
        """Uninstall's side: a server holds the barrier shared, the
        destructive command probes it exclusively."""
        with _handle(lock_path) as server, _handle(lock_path) as uninstall:
            portalocker.lock(server, portalocker.LOCK_SH)
            try:
                with pytest.raises(portalocker.AlreadyLocked) as excinfo:
                    portalocker.lock(uninstall, portalocker.LOCK_EX | portalocker.LOCK_NB)
                _assert_exclusive_contention(excinfo.value)
            finally:
                portalocker.unlock(server)

    def test_shared_contends_with_exclusive_holder(self, lock_path: Path) -> None:
        """The reverse: uninstall holds the barrier exclusively, a starting
        server asks for it shared."""
        with _handle(lock_path) as uninstall, _handle(lock_path) as server:
            portalocker.lock(uninstall, portalocker.LOCK_EX)
            try:
                with pytest.raises(portalocker.AlreadyLocked) as excinfo:
                    portalocker.lock(server, portalocker.LOCK_SH | portalocker.LOCK_NB)
                _assert_win32_contention(excinfo.value)
            finally:
                portalocker.unlock(uninstall)

    def test_two_shared_handles_coexist(self, lock_path: Path) -> None:
        """``_acquire_barrier`` deliberately has no intra-process lock layer
        because several shared holders in one process must coexist."""
        with _handle(lock_path) as first, _handle(lock_path) as second:
            portalocker.lock(first, portalocker.LOCK_SH)
            try:
                portalocker.lock(second, portalocker.LOCK_SH | portalocker.LOCK_NB)
                portalocker.unlock(second)
            finally:
                portalocker.unlock(first)
