"""Tests for memtomem.context._atomic — crash safety + explicit mode."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from memtomem.context._atomic import atomic_write_bytes, atomic_write_text


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
