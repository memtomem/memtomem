"""Tests for :mod:`memtomem._runtime_paths` (#412).

Runtime files (pid, flock) resolve to ``$XDG_RUNTIME_DIR/memtomem`` when
the platform provides one, and a per-user temp subdir otherwise. The
resolver is side-effect free — ``runtime_dir()`` must NOT create the
directory; ``ensure_runtime_dir()`` is the explicit opt-in.
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


class TestRuntimeDir:
    def test_uses_xdg_runtime_dir_when_set(self, tmp_path, monkeypatch):
        xdg = tmp_path / "xdg"
        xdg.mkdir()
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(xdg))

        assert runtime_dir() == xdg / "memtomem"

    def test_does_not_create_directory(self, tmp_path, monkeypatch):
        """Plain ``runtime_dir()`` must be a pure path resolver. Inventory
        walks rely on it to probe for ``server.pid`` without leaving an
        empty subdir behind on machines where the runtime path doesn't
        exist yet."""
        xdg = tmp_path / "xdg"
        xdg.mkdir()
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(xdg))

        result = runtime_dir()
        assert not result.exists(), "runtime_dir() must not mkdir"

    def test_falls_back_to_tempdir_when_xdg_unset(self, monkeypatch):
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)

        result = runtime_dir()

        assert result.parent == Path(tempfile.gettempdir())
        assert result.name.startswith("memtomem-")

    def test_falls_back_when_xdg_points_at_nonexistent_dir(self, tmp_path, monkeypatch):
        """A user who exports ``XDG_RUNTIME_DIR`` but whose shell never
        materialized it (some remote-ssh configs) must still get a
        working path, not a mkdir against a non-existent parent."""
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "does-not-exist"))

        result = runtime_dir()

        assert result.parent == Path(tempfile.gettempdir())


class TestEnsureRuntimeDir:
    def test_creates_directory_with_owner_only_mode(self, tmp_path, monkeypatch):
        xdg = tmp_path / "xdg"
        xdg.mkdir()
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(xdg))

        d = ensure_runtime_dir()

        assert d.exists() and d.is_dir()
        # mode & 0o777 masks off file-type bits
        assert stat.S_IMODE(d.stat().st_mode) == 0o700

    def test_idempotent_does_not_fail_on_existing_dir(self, tmp_path, monkeypatch):
        xdg = tmp_path / "xdg"
        xdg.mkdir()
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(xdg))

        ensure_runtime_dir()
        # Second call must not raise FileExistsError.
        ensure_runtime_dir()

    def test_does_not_chmod_existing_dir(self, tmp_path, monkeypatch):
        """A pre-existing dir (maybe created by ``root`` via ``sudo``)
        keeps its own permissions — we never silently downgrade."""
        xdg = tmp_path / "xdg"
        xdg.mkdir()
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(xdg))

        (xdg / "memtomem").mkdir(mode=0o755)

        d = ensure_runtime_dir()

        assert stat.S_IMODE(d.stat().st_mode) == 0o755


class TestServerPidPath:
    def test_resolves_to_runtime_dir_server_pid(self, tmp_path, monkeypatch):
        xdg = tmp_path / "xdg"
        xdg.mkdir()
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(xdg))

        assert server_pid_path() == xdg / "memtomem" / "server.pid"

    def test_does_not_create_parent(self, tmp_path, monkeypatch):
        xdg = tmp_path / "xdg"
        xdg.mkdir()
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
