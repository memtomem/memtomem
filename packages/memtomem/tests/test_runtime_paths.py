"""Tests for :mod:`memtomem._runtime_paths` (#412).

Runtime files (pid, flock) resolve to ``$XDG_RUNTIME_DIR/memtomem`` when
the platform provides one, and a per-user temp subdir otherwise. The
resolver is side-effect free — ``runtime_dir()`` must NOT create the
directory; ``ensure_runtime_dir()`` is the explicit opt-in and enforces
the security contract (symlink / owner / mode).
"""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

import pytest

from memtomem._runtime_paths import (
    ensure_runtime_dir,
    legacy_server_pid_path,
    runtime_dir,
    server_pid_path,
)


def _make_safe_xdg(tmp_path: Path) -> Path:
    """Create an ``$XDG_RUNTIME_DIR``-shaped base under ``tmp_path``.

    ``Path.mkdir`` applies umask to the mode, and system umask may strip
    owner-only bits we wanted. ``chmod`` after the fact neutralizes that
    so the happy-path tests don't depend on the developer's umask.
    """
    xdg = tmp_path / "xdg"
    xdg.mkdir()
    os.chmod(xdg, 0o700)
    return xdg


class TestRuntimeDir:
    def test_uses_xdg_runtime_dir_when_set(self, tmp_path, monkeypatch):
        xdg = _make_safe_xdg(tmp_path)
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(xdg))

        assert runtime_dir() == xdg / "memtomem"

    def test_does_not_create_directory(self, tmp_path, monkeypatch):
        """Plain ``runtime_dir()`` must be a pure path resolver. Inventory
        walks rely on it to probe for ``server.pid`` without leaving an
        empty subdir behind on machines where the runtime path doesn't
        exist yet."""
        xdg = _make_safe_xdg(tmp_path)
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(xdg))

        result = runtime_dir()
        assert not result.exists(), "runtime_dir() must not mkdir"

    def test_falls_back_to_tempdir_when_xdg_unset(self, monkeypatch):
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)

        result = runtime_dir()

        assert result.parent == Path(tempfile.gettempdir())
        assert result.name.startswith("memtomem-")

    def test_falls_back_when_xdg_path_is_missing_or_stale(self, tmp_path, monkeypatch):
        """``XDG_RUNTIME_DIR`` is exported but the target isn't on disk —
        either it was never materialised (some remote-ssh configs) or it
        has been reaped after a session ended. Both produce a missing
        directory at stat time and must fall through to the tempdir form,
        not mkdir against a non-existent parent."""
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "does-not-exist"))

        result = runtime_dir()

        assert result.parent == Path(tempfile.gettempdir())

    def test_falls_back_when_xdg_is_symlink(self, tmp_path, monkeypatch):
        """Attacker on a shared host pre-creates ``$XDG_RUNTIME_DIR`` as a
        symlink into the user's home. ``_is_safe_dir`` must reject it —
        the whole point of ``follow_symlinks=False`` is to catch this
        before ``ensure_runtime_dir`` follows the link and writes the
        pid file somewhere the user didn't expect."""
        real = _make_safe_xdg(tmp_path)
        link = tmp_path / "xdg-link"
        os.symlink(real, link)
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(link))

        result = runtime_dir()

        assert result.parent == Path(tempfile.gettempdir()), (
            "symlinked XDG base must fall through to tempdir, not be followed"
        )

    def test_falls_back_when_xdg_is_world_readable(self, tmp_path, monkeypatch):
        """``XDG_RUNTIME_DIR=/tmp`` (world-writable) is the canonical
        misconfiguration: falling through to the per-user tempdir form
        still gives owner-only ``memtomem-{uid}``, while using the bad
        base would place ``server.pid`` in a directory anyone can list."""
        loose = tmp_path / "loose-xdg"
        loose.mkdir()
        os.chmod(loose, 0o755)  # group + world read/execute
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(loose))

        result = runtime_dir()

        assert result.parent == Path(tempfile.gettempdir())

    @pytest.mark.skipif(not hasattr(os, "geteuid"), reason="owner check is POSIX-only")
    def test_falls_back_when_xdg_owner_mismatch(self, tmp_path, monkeypatch):
        """Stubbing ``geteuid`` to return a different uid simulates a
        root-owned ``$XDG_RUNTIME_DIR`` left over from a ``sudo`` run.
        The owner check must see the mismatch and fall through."""
        xdg = _make_safe_xdg(tmp_path)
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(xdg))
        real_uid = os.geteuid()
        monkeypatch.setattr(os, "geteuid", lambda: real_uid + 1)

        result = runtime_dir()

        assert result.parent == Path(tempfile.gettempdir())


class TestEnsureRuntimeDir:
    def test_creates_directory_with_owner_only_mode(self, tmp_path, monkeypatch):
        xdg = _make_safe_xdg(tmp_path)
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(xdg))

        d = ensure_runtime_dir()

        assert d.exists() and d.is_dir()
        assert stat.S_IMODE(d.stat().st_mode) == 0o700

    def test_explicit_chmod_survives_wild_umask(self, tmp_path, monkeypatch):
        """``mkdir(mode=0o700)`` is still subject to umask masking. A
        pathological ``umask 0o177`` would clear the owner-exec bit and
        silently produce an unusable 0o600 dir. The belt-and-suspenders
        explicit ``chmod`` neutralizes that."""
        xdg = _make_safe_xdg(tmp_path)
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(xdg))
        old_umask = os.umask(0o177)
        try:
            d = ensure_runtime_dir()
        finally:
            os.umask(old_umask)

        assert stat.S_IMODE(d.stat().st_mode) == 0o700

    def test_idempotent_does_not_fail_on_existing_dir(self, tmp_path, monkeypatch):
        xdg = _make_safe_xdg(tmp_path)
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(xdg))

        ensure_runtime_dir()
        # Second call must not raise FileExistsError, must re-validate,
        # must return the same path.
        ensure_runtime_dir()

    def test_refuses_existing_symlink(self, tmp_path, monkeypatch):
        """Symlink-at-the-runtime-path attack: attacker symlinks
        ``$XDG_RUNTIME_DIR/memtomem`` into the user's home. Pre-M1 fix,
        ``mkdir(exist_ok=True)`` followed the link silently and
        ``open(server_pid_path(), "w")`` wrote into the target. Now the
        validator raises before we touch the file."""
        xdg = _make_safe_xdg(tmp_path)
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(xdg))
        target = tmp_path / "real-target"
        target.mkdir(mode=0o700)
        os.symlink(target, xdg / "memtomem")

        with pytest.raises(PermissionError, match="symlink"):
            ensure_runtime_dir()

    def test_refuses_existing_loose_mode(self, tmp_path, monkeypatch):
        """Regression for M3 in the #412 review: a pre-existing dir at
        mode 0o755 used to be silently accepted, leaking the pid file
        into a group/world-readable location. The contract now enforces
        0o700 and surfaces remediation ``rm -rf`` in the error."""
        xdg = _make_safe_xdg(tmp_path)
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(xdg))
        (xdg / "memtomem").mkdir(mode=0o755)
        os.chmod(xdg / "memtomem", 0o755)  # neutralize umask

        with pytest.raises(PermissionError, match="unsafe permissions"):
            ensure_runtime_dir()

    @pytest.mark.skipif(not hasattr(os, "geteuid"), reason="owner check is POSIX-only")
    def test_refuses_existing_wrong_owner(self, tmp_path, monkeypatch):
        """Stub ``geteuid`` to simulate a ``root``-owned leftover from a
        prior ``sudo mm …`` run. The validator must raise with a
        clean-up hint rather than proceed against a dir we don't own.

        We route through the ``TMPDIR`` fallback rather than XDG because
        ``runtime_dir()`` also consults ``geteuid()`` for the XDG safety
        gate — a uid-stub applied before resolution would flip the whole
        path to fallback before ``ensure_runtime_dir`` ever stat'd the
        memtomem subdir, missing the existing-dir branch we want to
        cover. Pre-creating the fallback subdir at the stubbed uid's
        expected name lets us exercise that branch end-to-end.
        """
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        tmp_tmp = tmp_path / "tmp"
        tmp_tmp.mkdir()
        os.chmod(tmp_tmp, 0o700)
        monkeypatch.setenv("TMPDIR", str(tmp_tmp))
        tempfile.tempdir = None  # tempfile caches ``gettempdir()`` — invalidate

        real_uid = os.geteuid()
        stubbed_uid = real_uid + 1
        # Pre-create the path that ``runtime_dir()`` will return under
        # the stubbed uid. Owned by us (``st_uid == real_uid``) but the
        # stubbed ``geteuid()`` returns ``stubbed_uid`` → mismatch.
        (tmp_tmp / f"memtomem-{stubbed_uid}").mkdir(mode=0o700)
        monkeypatch.setattr(os, "geteuid", lambda: stubbed_uid)

        try:
            with pytest.raises(PermissionError, match="owned by uid"):
                ensure_runtime_dir()
        finally:
            tempfile.tempdir = None  # reset cache for subsequent tests

    def test_refuses_non_directory(self, tmp_path, monkeypatch):
        """A regular file where we expected a directory — unlikely but
        the validator should refuse rather than try to ``mkdir`` over
        it (which would ``FileExistsError`` anyway, just less clearly)."""
        xdg = _make_safe_xdg(tmp_path)
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(xdg))
        (xdg / "memtomem").write_text("accidentally a file")

        with pytest.raises(PermissionError, match="not a directory"):
            ensure_runtime_dir()


class TestServerPidPath:
    def test_resolves_to_runtime_dir_server_pid(self, tmp_path, monkeypatch):
        xdg = _make_safe_xdg(tmp_path)
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(xdg))

        assert server_pid_path() == xdg / "memtomem" / "server.pid"

    def test_does_not_create_parent(self, tmp_path, monkeypatch):
        xdg = _make_safe_xdg(tmp_path)
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(xdg))

        server_pid_path()

        assert not (xdg / "memtomem").exists(), (
            "server_pid_path() is a path resolver; use ensure_runtime_dir() "
            "explicitly when opening the file"
        )


class TestLegacyServerPidPath:
    def test_evaluates_home_lazily(self, tmp_path, monkeypatch):
        """Import-time ``Path.home()`` would capture the developer's
        real home and leak across fixtures; the function must re-read
        ``$HOME`` every call."""
        monkeypatch.setenv("HOME", str(tmp_path))

        assert legacy_server_pid_path() == tmp_path / ".memtomem" / ".server.pid"


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="uid fallback only meaningful on POSIX")
class TestUidFallback:
    def test_fallback_dir_contains_effective_uid(self, monkeypatch):
        """On systems without ``$XDG_RUNTIME_DIR``, the dir name
        includes ``geteuid()`` so a shared ``/tmp`` doesn't silently
        collide between users."""
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)

        result = runtime_dir()

        assert result.name == f"memtomem-{os.geteuid()}"
