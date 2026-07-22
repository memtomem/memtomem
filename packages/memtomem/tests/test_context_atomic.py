"""Tests for memtomem.context._atomic — crash safety + explicit mode."""

from __future__ import annotations

import errno
import os
import stat
import sys
import time
from pathlib import Path

import pytest

from memtomem.context import _atomic as _atomic_mod
from memtomem.context._atomic import (
    StrictTreeError,
    _file_lock,
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
