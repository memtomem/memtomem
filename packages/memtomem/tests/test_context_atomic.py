"""Tests for memtomem.context._atomic — crash safety + explicit mode."""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import stat
import sys
import time
from pathlib import Path

import portalocker
import pytest

from memtomem.context import _atomic as _atomic_mod
from memtomem.context._atomic import (
    StrictTreeError,
    _file_lock,
    async_file_lock,
    _fsync_fd,
    _lock_path_for,
    atomic_write_bytes,
    atomic_write_text,
    copy_tree_strict,
    fsync_dir,
    hardlink_tree_strict,
    iter_installed_files,
    link_or_copy_file,
    rename_no_replace,
    validate_tree_strict,
    write_tree_payload,
)


def _list_tmp_siblings(path: Path) -> list[Path]:
    """Tempfiles created by atomic_write live in path.parent with a `.<name>.` prefix."""
    return sorted(p for p in path.parent.iterdir() if p.name.startswith(f".{path.name}."))


def test_atomic_write_text_writes_content(tmp_path: Path) -> None:
    target = tmp_path / "out.txt"
    atomic_write_text(target, "hello world")
    assert target.read_text(encoding="utf-8") == "hello world"


def test_atomic_write_bytes_writes_content(tmp_path: Path) -> None:
    target = tmp_path / "out.bin"
    atomic_write_bytes(target, b"\x00\x01\x02raw")
    assert target.read_bytes() == b"\x00\x01\x02raw"


def test_atomic_write_creates_parent_dirs(tmp_path: Path) -> None:
    target = tmp_path / "sub" / "deep" / "out.txt"
    atomic_write_text(target, "nested")
    assert target.read_text(encoding="utf-8") == "nested"


def test_atomic_write_replaces_existing_file(tmp_path: Path) -> None:
    target = tmp_path / "out.txt"
    target.write_text("old", encoding="utf-8")
    atomic_write_text(target, "new")
    assert target.read_text(encoding="utf-8") == "new"


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX file mode (stat.S_IMODE) — Windows ignores POSIX permission bits",
)
def test_atomic_write_explicit_mode_0o600(tmp_path: Path) -> None:
    """Mode is applied via fchmod — independent of process umask."""
    target = tmp_path / "secret.json"
    old_umask = os.umask(0o077)
    try:
        atomic_write_text(target, "{}")
    finally:
        os.umask(old_umask)

    perms = stat.S_IMODE(target.stat().st_mode)
    assert perms == 0o600, f"expected 0o600, got {oct(perms)}"


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX file mode (stat.S_IMODE) — Windows ignores POSIX permission bits",
)
def test_atomic_write_respects_custom_mode(tmp_path: Path) -> None:
    target = tmp_path / "public.md"
    atomic_write_text(target, "readable", mode=0o644)
    assert stat.S_IMODE(target.stat().st_mode) == 0o644


def test_crash_between_open_and_replace_preserves_old(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If os.replace raises, the pre-existing file is untouched and no .tmp sibling remains."""
    target = tmp_path / "settings.json"
    target.write_text('{"original": true}', encoding="utf-8")

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated crash mid-replace")

    monkeypatch.setattr("memtomem.context._atomic.os.replace", _boom)

    with pytest.raises(OSError, match="simulated crash"):
        atomic_write_text(target, '{"new": true}')

    assert target.read_text(encoding="utf-8") == '{"original": true}'
    assert _list_tmp_siblings(target) == []


def test_crash_mid_payload_preserves_old(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """If the tempfile write raises partway, target is unchanged and tempfile is cleaned."""
    target = tmp_path / "settings.json"
    target.write_text('{"original": true}', encoding="utf-8")

    real_fdopen = os.fdopen

    class _ExplodingFile:
        def __init__(self, real_file: object) -> None:
            self._real = real_file

        def __enter__(self) -> "_ExplodingFile":
            return self

        def __exit__(self, *_a: object) -> None:
            self._real.__exit__(None, None, None)  # type: ignore[attr-defined]

        def write(self, _data: bytes) -> int:
            raise OSError("simulated mid-write crash")

        def flush(self) -> None:
            pass

        def fileno(self) -> int:
            return self._real.fileno()  # type: ignore[attr-defined]

    def _fake_fdopen(fd: int, mode: str, **kwargs: object) -> _ExplodingFile:
        return _ExplodingFile(real_fdopen(fd, mode, **kwargs))

    monkeypatch.setattr("memtomem.context._atomic.os.fdopen", _fake_fdopen)

    with pytest.raises(OSError, match="simulated mid-write"):
        atomic_write_text(target, '{"new": true}')

    assert target.read_text(encoding="utf-8") == '{"original": true}'
    assert _list_tmp_siblings(target) == []


def test_crash_with_no_preexisting_target_cleans_tempfile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When target does not exist yet, a crash mid-write still cleans up the tempfile."""
    target = tmp_path / "never-written.json"

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated")

    monkeypatch.setattr("memtomem.context._atomic.os.replace", _boom)

    with pytest.raises(OSError):
        atomic_write_text(target, "{}")

    assert not target.exists()
    assert _list_tmp_siblings(target) == []


class TestFileLockTimeout:
    """``_file_lock(timeout=...)`` bounds acquisition instead of blocking
    forever (#1145 review) — needed where the lock is taken from a context that
    must not hang (an async handler's worker thread)."""

    def test_acquires_immediately_when_free(self, tmp_path: Path) -> None:
        lock = _lock_path_for(tmp_path / "data.json")
        # A free lock with a timeout acquires without raising.
        with _file_lock(lock, timeout=5.0):
            pass
        # And again, proving it released cleanly.
        with _file_lock(lock, timeout=5.0):
            pass

    def test_timeout_raises_when_held(self, tmp_path: Path) -> None:
        # portalocker locks are per-open-file-description, so a second
        # acquisition (separate fd) in the SAME process contends — mirroring the
        # cross-process case the bound protects. Holding the lock and then
        # requesting it with a short timeout must raise TimeoutError, not hang.
        lock = _lock_path_for(tmp_path / "data.json")
        with _file_lock(lock):
            start = time.monotonic()
            with pytest.raises(TimeoutError):
                with _file_lock(lock, timeout=0.2):
                    pass
            elapsed = time.monotonic() - start
        # It actually polled to the deadline (not an instant grant) and the
        # bound fired (not an indefinite block).
        assert 0.1 <= elapsed < 5.0

    def test_default_is_still_blocking(self, tmp_path: Path) -> None:
        # No timeout → unchanged behavior: a free lock acquires (the indefinite
        # block only matters under contention, which the held-lock test covers).
        lock = _lock_path_for(tmp_path / "data.json")
        with _file_lock(lock):
            pass


class _BodyFailure(BaseException):
    """Body exception outside the ``Exception`` hierarchy.

    ``except Exception`` around the ``yield`` would let an unlock failure
    replace this one, so using it (and ``CancelledError`` in the async twin)
    is what makes the ``except BaseException`` contract mutation-proof.
    """


def _lock_exc(cause: BaseException | None) -> portalocker.LockException:
    """A bare ``LockException`` chained to *cause*, the shape portalocker's
    backends raise for a lock call that failed for a non-contention reason."""
    exc = portalocker.LockException("lock failed")
    exc.__cause__ = cause
    return exc


class _FakePywinError(Exception):
    """``pywintypes.error`` stand-in: an ``Exception`` carrying ``winerror``.

    pywin32 is absent on POSIX, so the real type cannot be raised here; what
    matters is the *shape* portalocker 3.x re-raises raw — not an ``OSError``,
    so only the widened catch set sees it at all.
    """

    def __init__(self, winerror: int) -> None:
        super().__init__(winerror, "win32 lock failure")
        self.winerror = winerror


class _RaisingLock:
    """``portalocker.lock`` replacement raising a fixed exception each call."""

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc
        self.calls = 0

    def __call__(self, *args: object, **kwargs: object) -> None:
        self.calls += 1
        raise self.exc


class _RecordingUnlock:
    """``portalocker.unlock`` replacement: counts calls, optionally raises."""

    def __init__(self, exc: BaseException | None = None) -> None:
        self.exc = exc
        self.calls = 0

    def __call__(self, *args: object, **kwargs: object) -> None:
        self.calls += 1
        if self.exc is not None:
            raise self.exc


class TestFileLockFailureClassification:
    """A lock call can fail for two unrelated reasons and #2229 conflated them:
    every ``LockException`` was polled to the deadline and then reported as
    ``TimeoutError`` "held by another process". Contention is retryable;
    an ``EIO``/``ENOLCK``/NFS-``EOFError`` lock-call failure never is, and
    advertising it as busy sends a retrying client (and its operator) after a
    holder that does not exist."""

    def test_io_failure_raises_oserror_without_burning_the_budget(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lock = _lock_path_for(tmp_path / "data.json")
        cause = OSError(errno.EIO, "input/output error")
        lock_stub = _RaisingLock(_lock_exc(cause))
        unlock = _RecordingUnlock()
        monkeypatch.setattr(_atomic_mod.portalocker, "lock", lock_stub)
        monkeypatch.setattr(_atomic_mod.portalocker, "unlock", unlock)

        start = time.monotonic()
        with pytest.raises(OSError) as excinfo:
            with _file_lock(lock, timeout=30.0):
                pytest.fail("body must not run when acquisition failed")
        elapsed = time.monotonic() - start

        assert not isinstance(excinfo.value, TimeoutError)
        assert excinfo.value.errno == errno.EIO
        assert excinfo.value.filename == str(lock)
        # Raised on the first failure, not after the 30s budget.
        assert elapsed < 5.0
        assert lock_stub.calls == 1
        # Nothing was acquired, so nothing may be released (#1145 contract).
        assert unlock.calls == 0

    def test_nfs_eoferror_becomes_a_pathful_oserror(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The NFS shape carries no errno at all, so the message is the only
        # thing an operator can act on — it must name the lock.
        lock = _lock_path_for(tmp_path / "data.json")
        unlock = _RecordingUnlock()
        monkeypatch.setattr(_atomic_mod.portalocker, "lock", _RaisingLock(_lock_exc(EOFError())))
        monkeypatch.setattr(_atomic_mod.portalocker, "unlock", unlock)

        with pytest.raises(OSError) as excinfo:
            with _file_lock(lock, timeout=30.0):
                pytest.fail("body must not run when acquisition failed")

        assert not isinstance(excinfo.value, TimeoutError)
        assert str(lock) in str(excinfo.value)
        assert unlock.calls == 0

    @pytest.mark.parametrize(
        "exc",
        [
            pytest.param(portalocker.AlreadyLocked("held"), id="already-locked"),
            pytest.param(BlockingIOError(errno.EAGAIN, "would block"), id="raw-blockingioerror"),
            pytest.param(OSError(errno.EACCES, "permission denied"), id="raw-eacces"),
            pytest.param(_lock_exc(OSError(errno.EAGAIN, "would block")), id="bare-with-cause"),
        ],
    )
    def test_contention_shapes_still_time_out(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exc: BaseException
    ) -> None:
        # Contention reaches the poll loop as more than one type across the
        # supported portalocker range (the floor is >=3.0): the classifier, not
        # the exception class, decides — so none of these may be reclassified
        # into the retry-proof bucket.
        lock = _lock_path_for(tmp_path / "data.json")
        monkeypatch.setattr(_atomic_mod.portalocker, "lock", _RaisingLock(exc))
        monkeypatch.setattr(_atomic_mod.portalocker, "unlock", _RecordingUnlock())

        with pytest.raises(TimeoutError, match="held by another process"):
            with _file_lock(lock, timeout=0.2):
                pytest.fail("body must not run when acquisition failed")


class TestFileLockReleaseDoesNotMaskBody:
    """``finally: unlock(fp)`` let a release failure replace the body's
    exception (#2229): the caller's classified ``except`` arms then saw a raw
    ``OSError`` from the unlock instead of the error they were written for."""

    @pytest.mark.parametrize(
        "unlock_exc",
        [
            pytest.param(OSError(errno.EIO, "unlock failed"), id="posix-oserror"),
            pytest.param(portalocker.LockException("unlock denied"), id="windows-lockexception"),
        ],
    )
    def test_body_exception_survives_a_failing_unlock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unlock_exc: BaseException
    ) -> None:
        lock = _lock_path_for(tmp_path / "data.json")
        monkeypatch.setattr(_atomic_mod.portalocker, "unlock", _RecordingUnlock(unlock_exc))

        with pytest.raises(_BodyFailure):
            with _file_lock(lock, timeout=5.0):
                raise _BodyFailure("what the caller actually needs to see")

    def test_the_suppressed_unlock_error_is_logged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Suppressing is not ignoring: the release failure still has to be
        # findable in the log.
        lock = _lock_path_for(tmp_path / "data.json")
        monkeypatch.setattr(
            _atomic_mod.portalocker, "unlock", _RecordingUnlock(OSError(errno.EIO, "unlock failed"))
        )

        with caplog.at_level(logging.WARNING, logger=_atomic_mod.logger.name):
            with pytest.raises(_BodyFailure):
                with _file_lock(lock, timeout=5.0):
                    raise _BodyFailure("boom")

        assert any(str(lock) in record.getMessage() for record in caplog.records)

    def test_body_exception_survives_a_failing_close(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Closing the descriptor is what actually drops the lock, so it always
        # runs — but on the way out of a failed body it must not become the
        # exception the caller sees any more than the unlock may.
        lock = _lock_path_for(tmp_path / "data.json")
        real_fdopen = os.fdopen

        def _fdopen(*args: object, **kwargs: object):  # noqa: ANN202
            fp = real_fdopen(*args, **kwargs)  # type: ignore[arg-type]
            fp.close = _RecordingUnlock(OSError(errno.EIO, "close failed"))  # type: ignore[method-assign]
            return fp

        monkeypatch.setattr(_atomic_mod.os, "fdopen", _fdopen)

        with pytest.raises(_BodyFailure):
            with _file_lock(lock, timeout=5.0):
                raise _BodyFailure("what the caller actually needs to see")

    def test_unlock_failure_on_the_success_path_still_propagates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Deliberately unchanged: the guarded work already succeeded, so there
        # is no exception to protect and nothing to relabel — the release
        # failure is the only thing that went wrong and must stay visible.
        lock = _lock_path_for(tmp_path / "data.json")
        monkeypatch.setattr(
            _atomic_mod.portalocker, "unlock", _RecordingUnlock(OSError(errno.EIO, "unlock failed"))
        )

        with pytest.raises(OSError, match="unlock failed"):
            with _file_lock(lock, timeout=5.0):
                pass


class TestAsyncFileLockClassification:
    """``async_file_lock`` carried both defects character-for-character; its
    contract is the sync one plus cancellation."""

    @pytest.mark.asyncio
    async def test_io_failure_raises_oserror_immediately(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lock = _lock_path_for(tmp_path / "data.json")
        lock_stub = _RaisingLock(_lock_exc(OSError(errno.ENOLCK, "no locks available")))
        unlock = _RecordingUnlock()
        monkeypatch.setattr(_atomic_mod.portalocker, "lock", lock_stub)
        monkeypatch.setattr(_atomic_mod.portalocker, "unlock", unlock)

        start = time.monotonic()
        with pytest.raises(OSError) as excinfo:
            async with async_file_lock(lock, timeout=30.0):
                pytest.fail("body must not run when acquisition failed")
        elapsed = time.monotonic() - start

        assert not isinstance(excinfo.value, TimeoutError)
        assert excinfo.value.errno == errno.ENOLCK
        assert excinfo.value.filename == str(lock)
        assert elapsed < 5.0
        assert lock_stub.calls == 1
        assert unlock.calls == 0

    @pytest.mark.asyncio
    async def test_contention_still_times_out(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lock = _lock_path_for(tmp_path / "data.json")
        monkeypatch.setattr(
            _atomic_mod.portalocker, "lock", _RaisingLock(portalocker.AlreadyLocked("held"))
        )
        monkeypatch.setattr(_atomic_mod.portalocker, "unlock", _RecordingUnlock())

        with pytest.raises(TimeoutError, match="held by another process"):
            async with async_file_lock(lock, timeout=0.2):
                pytest.fail("body must not run when acquisition failed")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "unlock_exc",
        [
            pytest.param(OSError(errno.EIO, "unlock failed"), id="posix-oserror"),
            pytest.param(portalocker.LockException("unlock denied"), id="windows-lockexception"),
        ],
    )
    async def test_body_exception_survives_a_failing_unlock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unlock_exc: BaseException
    ) -> None:
        lock = _lock_path_for(tmp_path / "data.json")
        monkeypatch.setattr(_atomic_mod.portalocker, "unlock", _RecordingUnlock(unlock_exc))

        with pytest.raises(_BodyFailure):
            async with async_file_lock(lock, timeout=5.0):
                raise _BodyFailure("what the caller actually needs to see")

    @pytest.mark.asyncio
    async def test_cancellation_survives_a_failing_unlock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ``CancelledError`` is a ``BaseException``: an ``except Exception``
        # around the ``yield`` would let the unlock error replace it, and the
        # awaiting caller would see an I/O error instead of its own cancel.
        lock = _lock_path_for(tmp_path / "data.json")
        monkeypatch.setattr(
            _atomic_mod.portalocker, "unlock", _RecordingUnlock(OSError(errno.EIO, "unlock failed"))
        )
        started = asyncio.Event()

        async def holder() -> None:
            async with async_file_lock(lock, timeout=5.0):
                started.set()
                await asyncio.sleep(60)

        task = asyncio.create_task(holder())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    @pytest.mark.asyncio
    async def test_body_exception_survives_a_failing_close(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lock = _lock_path_for(tmp_path / "data.json")
        real_fdopen = os.fdopen

        def _fdopen(*args: object, **kwargs: object):  # noqa: ANN202
            fp = real_fdopen(*args, **kwargs)  # type: ignore[arg-type]
            fp.close = _RecordingUnlock(OSError(errno.EIO, "close failed"))  # type: ignore[method-assign]
            return fp

        monkeypatch.setattr(_atomic_mod.os, "fdopen", _fdopen)

        with pytest.raises(_BodyFailure):
            async with async_file_lock(lock, timeout=5.0):
                raise _BodyFailure("what the caller actually needs to see")

    @pytest.mark.asyncio
    async def test_unlock_failure_on_the_success_path_still_propagates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lock = _lock_path_for(tmp_path / "data.json")
        monkeypatch.setattr(
            _atomic_mod.portalocker, "unlock", _RecordingUnlock(OSError(errno.EIO, "unlock failed"))
        )

        with pytest.raises(OSError, match="unlock failed"):
            async with async_file_lock(lock, timeout=5.0):
                pass


class TestFileLockBlockingBranchClassification:
    """``timeout=None`` (the default, used by ``context/projects.py`` and the
    memory-index doctor) hands the acquire to portalocker's own blocking mode.
    It has no poll loop to protect, but the failure contract is the same one:
    a lock-call I/O failure must still arrive as ``OSError``, not as the raw
    ``LockException`` that no caller in the tree can catch."""

    def test_io_failure_is_normalized_without_a_timeout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lock = _lock_path_for(tmp_path / "data.json")
        unlock = _RecordingUnlock()
        monkeypatch.setattr(
            _atomic_mod.portalocker,
            "lock",
            _RaisingLock(_lock_exc(OSError(errno.EIO, "input/output error"))),
        )
        monkeypatch.setattr(_atomic_mod.portalocker, "unlock", unlock)

        with pytest.raises(OSError) as excinfo:
            with _file_lock(lock):
                pytest.fail("body must not run when acquisition failed")

        assert not isinstance(excinfo.value, portalocker.LockException)
        assert excinfo.value.errno == errno.EIO
        assert excinfo.value.filename == str(lock)
        assert unlock.calls == 0

    def test_contention_keeps_its_own_type_without_a_timeout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Windows' msvcrt backend can report a held lock instead of waiting
        # (#759). There is no budget here to turn that into a TimeoutError, and
        # callers that must not fail on contention pass a bound — so it must
        # pass through unchanged rather than be relabelled an I/O failure.
        lock = _lock_path_for(tmp_path / "data.json")
        monkeypatch.setattr(
            _atomic_mod.portalocker, "lock", _RaisingLock(portalocker.AlreadyLocked("held"))
        )
        monkeypatch.setattr(_atomic_mod.portalocker, "unlock", _RecordingUnlock())

        with pytest.raises(portalocker.AlreadyLocked):
            with _file_lock(lock):
                pytest.fail("body must not run when acquisition failed")


class TestFileLockRawWin32Shape:
    """portalocker 3.x — still inside the supported floor (``>=3.0``) —
    re-raises a non-lock-violation ``pywintypes.error`` **raw**, and that type
    is not an ``OSError``. The narrow catch set would let it escape
    unclassified, which is the very outcome #2229 is about."""

    def test_non_lock_violation_becomes_an_oserror(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lock = _lock_path_for(tmp_path / "data.json")
        monkeypatch.setattr(
            _atomic_mod,
            "LOCK_CALL_ERRORS_WIDE",
            (*_atomic_mod.LOCK_CALL_ERRORS_WIDE, _FakePywinError),
        )
        monkeypatch.setattr(_atomic_mod.portalocker, "lock", _RaisingLock(_FakePywinError(5)))
        monkeypatch.setattr(_atomic_mod.portalocker, "unlock", _RecordingUnlock())

        with pytest.raises(OSError) as excinfo:
            with _file_lock(lock, timeout=30.0):
                pytest.fail("body must not run when acquisition failed")

        assert not isinstance(excinfo.value, TimeoutError)
        assert str(lock) in str(excinfo.value)

    def test_lock_violation_is_still_contention(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ERROR_LOCK_VIOLATION leaked raw still means "held": polled, then
        # reported as contention, never as a path to repair.
        lock = _lock_path_for(tmp_path / "data.json")
        monkeypatch.setattr(
            _atomic_mod,
            "LOCK_CALL_ERRORS_WIDE",
            (*_atomic_mod.LOCK_CALL_ERRORS_WIDE, _FakePywinError),
        )
        monkeypatch.setattr(_atomic_mod.portalocker, "lock", _RaisingLock(_FakePywinError(33)))
        monkeypatch.setattr(_atomic_mod.portalocker, "unlock", _RecordingUnlock())

        with pytest.raises(TimeoutError, match="held by another process"):
            with _file_lock(lock, timeout=0.2):
                pytest.fail("body must not run when acquisition failed")


class TestIsCopySkippedRel:
    """``is_copy_skipped_rel`` is the single enumerator behind the pinned-path
    scan/copy parity (#1247) — its verdict must match the walkers' skip rules.

    The predicate ships with the privacy-gate fix, so it is imported inside
    each test: pre-fix that errors per-test without breaking file collection."""

    @pytest.mark.parametrize(
        "rel",
        [
            "foo.md.bak",  # DIRTY_SKIP_SUFFIXES at the top level
            "nested/dir/foo.md.bak",  # …and at depth
            "__pycache__/x.py",  # COPY_SKIP_NAMES as a leading dir part
            "a/.git/b",  # …as an interior dir part
            ".DS_Store",  # …as the filename itself
        ],
    )
    def test_true_for_skipped_rels(self, rel: str) -> None:
        from memtomem.context._atomic import is_copy_skipped_rel

        assert is_copy_skipped_rel(rel) is True

    @pytest.mark.parametrize(
        "rel",
        [
            "notes.md",
            "nested/dir/file.md",
            "bak",  # bare filename, not a .bak suffix
            "foo.bak.md",  # final suffix is .md — an interior .bak doesn't count
        ],
    )
    def test_false_for_kept_rels(self, rel: str) -> None:
        from memtomem.context._atomic import is_copy_skipped_rel

        assert is_copy_skipped_rel(rel) is False

    def test_agrees_with_installed_file_walker(self, tmp_path: Path) -> None:
        """Both directions on a real tree: predicate-skipped ⇔ walker-skipped,
        so the pinned-path enumerator cannot drift from the HEAD-path walker."""
        from memtomem.context._atomic import is_copy_skipped_rel

        verdicts = {
            "SKILL.md": False,
            "scripts/run.sh": False,
            "foo.md.bak": True,
            "nested/old.md.bak": True,
            "__pycache__/junk.pyc": True,
            ".DS_Store": True,
        }
        root = tmp_path / "asset"
        for rel in verdicts:
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"x")

        walked = {p.relative_to(root).as_posix() for p in iter_installed_files(root)}
        for rel, skipped in verdicts.items():
            assert is_copy_skipped_rel(rel) is skipped
            assert (rel in walked) is (not skipped)


class TestIterInstalledFilesFailClosed:
    """``iter_installed_files`` is FAIL-CLOSED: an unreadable directory or
    entry raises rather than silently shrinking the result. The privacy-gate
    source scan (``install._gate_a_scan_src_tree``) walks it to decide what to
    copy, so a silently-dropped file would be copied UNSCANNED. Callers that
    must survive an unreadable subtree (the read-only ``is_asset_dirty`` status
    walk) wrap the iteration themselves and degrade to dirty — they do not push
    a skip policy down into the walker (see test_dirty_digest)."""

    def test_raises_on_unreadable_subdir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "asset"
        (root / "scripts").mkdir(parents=True)
        (root / "SKILL.md").write_bytes(b"a")
        (root / "scripts" / "run.sh").write_bytes(b"b")

        orig_iterdir = Path.iterdir

        def failing_iterdir(self: Path):
            if self.name == "scripts":
                raise PermissionError(13, "Permission denied", str(self))
            return orig_iterdir(self)

        monkeypatch.setattr(Path, "iterdir", failing_iterdir)
        with pytest.raises(OSError):
            list(iter_installed_files(root))


class TestFsyncDir:
    """``fsync_dir`` is the rename-durability barrier and must NEVER raise: the
    rename has already succeeded, so aborting a completed, correct operation
    because we could not *prove* durability would trade a real failure for a
    hypothetical one (ADR-0030 §10)."""

    @pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot fsync a directory")
    def test_flushes_a_real_directory(self, tmp_path: Path) -> None:
        assert fsync_dir(tmp_path) is True

    def test_missing_path_returns_false(self, tmp_path: Path) -> None:
        assert fsync_dir(tmp_path / "nope") is False

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX open() semantics")
    def test_regular_file_returns_false(self, tmp_path: Path) -> None:
        target = tmp_path / "f.txt"
        target.write_text("x")
        # A file fd fsyncs fine on POSIX, so this must not be read as a
        # contract violation — either outcome is acceptable, but it must not
        # raise, which is the property that matters.
        assert fsync_dir(target) in (True, False)

    @pytest.mark.parametrize("err", [errno.EINVAL, errno.EPERM, errno.EACCES, errno.EBADF])
    def test_rejected_fsync_degrades_to_false(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, err: int
    ) -> None:
        """Network / tmpfs mounts reject directory fsync — degrade to
        process-crash consistency instead of failing the caller."""
        real_fsync = os.fsync

        def _fake(fd: int) -> None:
            raise OSError(err, os.strerror(err))

        monkeypatch.setattr(os, "fsync", _fake)
        assert fsync_dir(tmp_path) is False
        monkeypatch.setattr(os, "fsync", real_fsync)

    def test_open_failure_degrades_to_false(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _fake(*_args: object, **_kw: object) -> int:
            raise OSError(errno.EACCES, "denied")

        monkeypatch.setattr(os, "open", _fake)
        assert fsync_dir(tmp_path) is False

    def test_windows_returns_false_without_opening(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        opened: list[object] = []
        monkeypatch.setattr(_atomic_mod.sys, "platform", "win32")
        monkeypatch.setattr(os, "open", lambda *a, **k: opened.append(a))
        assert fsync_dir(tmp_path) is False
        assert not opened


class TestFullFsync:
    def test_full_fsync_writes_correct_bytes(self, tmp_path: Path) -> None:
        target = tmp_path / "v1.md"
        atomic_write_bytes(target, b"snapshot", full_fsync=True)
        assert target.read_bytes() == b"snapshot"

    @pytest.mark.skipif(sys.platform != "darwin", reason="F_FULLFSYNC is Darwin-only")
    def test_darwin_uses_f_fullfsync(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import fcntl

        calls: list[int] = []
        real = fcntl.fcntl

        def _spy(fd: int, op: int, *args: object) -> object:
            calls.append(op)
            return real(fd, op, *args)

        monkeypatch.setattr(fcntl, "fcntl", _spy)
        fd = os.open(os.devnull, os.O_RDONLY)
        try:
            _fsync_fd(fd, full=True)
        except OSError:
            pass  # /dev/null may reject the barrier — the CALL is the assertion
        finally:
            os.close(fd)
        assert getattr(fcntl, "F_FULLFSYNC", 51) in calls

    @pytest.mark.skipif(sys.platform != "darwin", reason="F_FULLFSYNC is Darwin-only")
    def test_falls_back_to_fsync_when_unsupported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Some network mounts ENOTSUP the barrier; a rejection must degrade to
        the plain fsync we would have done anyway, not fail the write."""
        import fcntl

        monkeypatch.setattr(
            fcntl, "fcntl", lambda *a, **k: (_ for _ in ()).throw(OSError(errno.ENOTSUP, "nope"))
        )
        fsynced: list[int] = []
        real_fsync = os.fsync
        monkeypatch.setattr(os, "fsync", lambda fd: (fsynced.append(fd), real_fsync(fd))[1])

        target = tmp_path / "v1.md"
        atomic_write_bytes(target, b"x", full_fsync=True)
        assert target.read_bytes() == b"x"
        assert fsynced


class TestWriteTreePayload:
    def test_materializes_nested_payload(self, tmp_path: Path) -> None:
        dst = tmp_path / "v1"
        write_tree_payload(dst, [("SKILL.md", b"top\n"), ("a/b/c.md", b"deep\n")], durable=True)
        assert (dst / "SKILL.md").read_bytes() == b"top\n"
        assert (dst / "a" / "b" / "c.md").read_bytes() == b"deep\n"

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
    def test_default_mode_is_0o644(self, tmp_path: Path) -> None:
        dst = tmp_path / "v1"
        write_tree_payload(dst, [("f.md", b"x")])
        assert stat.S_IMODE((dst / "f.md").stat().st_mode) == 0o644

    @pytest.mark.parametrize(
        "rel",
        [
            "../escape",
            "/abs",
            "a//b",
            "",
            ".",
            "..",
            "a/./b",
            "a/../b",
            "a\\b",
            "C:/x",
            "a/",
            # Windows DRIVE-RELATIVE: no separator, no ``..``, yet
            # ``PureWindowsPath('/base').joinpath('C:escape.txt')`` discards the
            # base entirely — the write lands outside the destination.
            "C:escape.txt",
            "safe/C:escape.txt",
            # NTFS alternate data stream, not a filename.
            "file:stream",
            # No OS accepts NUL in a filename, so without a preflight check it
            # raises from inside the write loop — after earlier entries landed.
            "bad\0.md",
            "dir\0/f.md",
        ],
    )
    def test_rejects_unsafe_relpath_writing_nothing(self, tmp_path: Path, rel: str) -> None:
        """Containment lives at the write primitive so no caller can forget it —
        and a rejection must leave the destination untouched, not half-built."""
        dst = tmp_path / "v1"
        with pytest.raises(ValueError):
            write_tree_payload(dst, [("ok.md", b"x"), (rel, b"bad")])
        assert not dst.exists()

    def test_rejects_duplicate_relpath(self, tmp_path: Path) -> None:
        dst = tmp_path / "v1"
        with pytest.raises(ValueError, match="duplicate"):
            write_tree_payload(dst, [("a.md", b"1"), ("a.md", b"2")])
        assert not dst.exists()

    def test_durable_fsyncs_created_dirs_deepest_first(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[Path] = []
        monkeypatch.setattr(_atomic_mod, "fsync_dir", lambda p: seen.append(p) or True)

        dst = tmp_path / "v1"
        write_tree_payload(dst, [("a/b/c.md", b"x")], durable=True)

        assert seen[-1] == dst  # parent last
        assert seen[0] == dst / "a" / "b"  # deepest first
        # EVERY intermediate ancestor, not just the file's immediate parent:
        # syncing only ``a/b`` leaves ``a``'s entry for ``b`` unflushed, so a
        # power cut can lose ``b`` from a tree already reported complete.
        assert set(seen) == {dst / "a" / "b", dst / "a", dst}
        assert [len(p.parts) for p in seen] == sorted((len(p.parts) for p in seen), reverse=True)

    def test_non_durable_skips_dir_fsync(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[Path] = []
        monkeypatch.setattr(_atomic_mod, "fsync_dir", lambda p: seen.append(p) or True)
        write_tree_payload(tmp_path / "v1", [("a/b.md", b"x")])
        assert seen == []


class TestRenameNoReplace:
    def test_moved_not_copied(self) -> None:
        """``skills._rename_no_replace`` must be the SAME object, not a copy —
        a second copy is how one call site silently loses the #1839 exclusivity
        contract."""
        from memtomem.context import skills

        assert skills._rename_no_replace is rename_no_replace

    def test_refuses_existing_destination(self, tmp_path: Path) -> None:
        src = tmp_path / "staging"
        src.mkdir()
        (src / "f.md").write_text("new")
        dst = tmp_path / "target"
        dst.mkdir()  # empty dir — plain os.rename WOULD replace this on POSIX
        (dst / "keep.md").write_text("old")

        with pytest.raises(OSError):
            rename_no_replace(src, dst)
        assert (dst / "keep.md").read_text() == "old"
        assert src.is_dir()

    def test_refuses_empty_existing_destination(self, tmp_path: Path) -> None:
        """The exact case plain ``os.rename`` would silently clobber."""
        src = tmp_path / "staging"
        src.mkdir()
        (src / "f.md").write_text("new")
        dst = tmp_path / "target"
        dst.mkdir()

        with pytest.raises(OSError):
            rename_no_replace(src, dst)
        assert list(dst.iterdir()) == []

    def test_cross_parent_refused(self, tmp_path: Path) -> None:
        src = tmp_path / "a" / "staging"
        src.mkdir(parents=True)
        dst = tmp_path / "b" / "target"
        dst.parent.mkdir(parents=True)
        with pytest.raises(OSError) as exc:
            rename_no_replace(src, dst)
        assert exc.value.errno == errno.EXDEV

    def test_promotes_into_absent_destination(self, tmp_path: Path) -> None:
        src = tmp_path / "staging"
        src.mkdir()
        (src / "f.md").write_text("new")
        dst = tmp_path / "target"

        rename_no_replace(src, dst)
        assert (dst / "f.md").read_text() == "new"
        assert not src.exists()


class TestStrictTreeWalkers:
    """The carry-then-delete strict walkers — REFUSE symlinks/special files
    (unlike copy_tree_atomic's skip-and-warn), guard their root, and keep the
    hardlink copy fallback durable (ADR-0030 PR-G4b + Codex gate)."""

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink semantics")
    def test_validate_refuses_a_nested_symlink(self, tmp_path: Path) -> None:
        root = tmp_path / "tree"
        (root / "sub").mkdir(parents=True)
        (root / "ok.md").write_text("x")
        (root / "sub" / "link.md").symlink_to(tmp_path / "outside.md")
        with pytest.raises(StrictTreeError) as exc:
            validate_tree_strict(root)
        assert exc.value.path == root / "sub" / "link.md"

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX FIFO")
    def test_validate_refuses_a_fifo(self, tmp_path: Path) -> None:
        root = tmp_path / "tree"
        root.mkdir()
        os.mkfifo(root / "pipe")
        with pytest.raises(StrictTreeError):
            validate_tree_strict(root)

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink semantics")
    def test_walkers_refuse_a_symlinked_root(self, tmp_path: Path) -> None:
        """Codex Major 1: a symlinked ROOT must be refused, not followed — the
        recursive walkers only lstat CHILDREN, so without a depth-zero guard the
        link's target would be walked, escaping the named tree."""
        real = tmp_path / "real"
        (real).mkdir()
        (real / "f.md").write_text("x")
        link = tmp_path / "link"
        link.symlink_to(real)
        for fn in (
            lambda: validate_tree_strict(link),
            lambda: copy_tree_strict(link, tmp_path / "cp"),
            lambda: hardlink_tree_strict(link, tmp_path / "hl"),
        ):
            with pytest.raises(StrictTreeError):
                fn()

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink semantics")
    def test_copy_strict_refuses_symlink_instead_of_skipping(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        (src / "real.md").write_text("x")
        (src / "link.md").symlink_to(tmp_path / "outside.md")
        with pytest.raises(StrictTreeError):
            copy_tree_strict(src, tmp_path / "dst")

    def test_copy_strict_mirrors_a_clean_tree(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        (src / "sub").mkdir(parents=True)
        (src / "a.md").write_text("A")
        (src / "sub" / "b.md").write_text("B")
        copy_tree_strict(src, tmp_path / "dst", durable=True)
        assert (tmp_path / "dst" / "a.md").read_text() == "A"
        assert (tmp_path / "dst" / "sub" / "b.md").read_text() == "B"
        # New inodes, not hardlinks.
        assert (tmp_path / "dst" / "a.md").stat().st_ino != (src / "a.md").stat().st_ino

    def test_hardlink_tree_links_files(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        (src / "v1").mkdir(parents=True)
        (src / "v1" / "s.md").write_text("hist")
        hardlink_tree_strict(src, tmp_path / "dst", durable=True)
        # Same inode (hardlink), dirs recreated.
        assert (tmp_path / "dst" / "v1" / "s.md").stat().st_ino == (
            src / "v1" / "s.md"
        ).stat().st_ino

    def test_link_or_copy_fallback_is_fsynced_when_durable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Codex Blocker 2: when os.link fails cross-device, the copy2 fallback
        must be fsynced under durable=True — else the swap deletes the original
        and a power loss loses the copied version history."""
        src = tmp_path / "src.md"
        src.write_text("history")
        dst = tmp_path / "dst.md"

        def _boom(*_a: object, **_k: object) -> None:
            raise OSError(errno.EXDEV, "cross-device")

        monkeypatch.setattr(os, "link", _boom)
        fsynced: list[str] = []
        real_fsync = _atomic_mod._fsync_fd

        def _spy(fd: int, *, full: bool) -> None:
            fsynced.append("full" if full else "plain")
            real_fsync(fd, full=full)

        monkeypatch.setattr(_atomic_mod, "_fsync_fd", _spy)
        link_or_copy_file(src, dst, durable=True)
        assert dst.read_text() == "history"
        assert "full" in fsynced  # the fallback copy was full_fsync'd
