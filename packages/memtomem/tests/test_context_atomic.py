"""Tests for memtomem.context._atomic — crash safety + explicit mode."""

from __future__ import annotations

import os
import stat
import sys
import time
from pathlib import Path

import pytest

from memtomem.context._atomic import (
    _file_lock,
    _lock_path_for,
    atomic_write_bytes,
    atomic_write_text,
    iter_installed_files,
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
