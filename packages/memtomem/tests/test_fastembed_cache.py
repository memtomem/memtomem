"""Tests for ``embedding.fastembed_cache.resolve_fastembed_cache_dir``.

The helper exists to keep the fastembed model snapshot out of macOS's
periodically-reaped ``/var/folders/.../T/`` tempdir (see the module
docstring for the failure mode). These tests pin the precedence rules and
the directory-creation contract; they do not exercise fastembed itself.
"""

from __future__ import annotations

import os
import stat
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from memtomem.embedding.fastembed_cache import resolve_fastembed_cache_dir
from .helpers import set_home


def test_default_is_under_memtomem_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("MEMTOMEM_FASTEMBED_CACHE", raising=False)
    monkeypatch.delenv("FASTEMBED_CACHE_PATH", raising=False)
    set_home(monkeypatch, tmp_path)

    resolved = resolve_fastembed_cache_dir()

    assert resolved == tmp_path / ".memtomem" / "cache" / "fastembed"
    assert resolved.is_dir()


def test_memtomem_env_takes_precedence_over_fastembed_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    memtomem_path = tmp_path / "mm-cache"
    fastembed_path = tmp_path / "fe-cache"
    monkeypatch.setenv("MEMTOMEM_FASTEMBED_CACHE", str(memtomem_path))
    monkeypatch.setenv("FASTEMBED_CACHE_PATH", str(fastembed_path))

    resolved = resolve_fastembed_cache_dir()

    assert resolved == memtomem_path
    assert resolved.is_dir()
    assert not fastembed_path.exists()


def test_fastembed_env_used_when_memtomem_env_unset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("MEMTOMEM_FASTEMBED_CACHE", raising=False)
    fastembed_path = tmp_path / "fe-cache"
    monkeypatch.setenv("FASTEMBED_CACHE_PATH", str(fastembed_path))

    resolved = resolve_fastembed_cache_dir()

    assert resolved == fastembed_path
    assert resolved.is_dir()


def test_empty_env_falls_through_to_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An empty-string env var must not pin the cache to ``Path("")`` —
    that would resolve to the cwd and silently scatter snapshots into
    whichever directory the user happened to launch ``mm`` from."""
    monkeypatch.setenv("MEMTOMEM_FASTEMBED_CACHE", "")
    monkeypatch.setenv("FASTEMBED_CACHE_PATH", "")
    set_home(monkeypatch, tmp_path)

    resolved = resolve_fastembed_cache_dir()

    assert resolved == tmp_path / ".memtomem" / "cache" / "fastembed"


def test_tilde_in_env_is_expanded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    set_home(monkeypatch, tmp_path)
    monkeypatch.setenv("MEMTOMEM_FASTEMBED_CACHE", "~/custom-cache")
    monkeypatch.delenv("FASTEMBED_CACHE_PATH", raising=False)

    resolved = resolve_fastembed_cache_dir()

    assert resolved == tmp_path / "custom-cache"
    assert resolved.is_dir()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX-only: simulates pwd.getpwuid failure mode of expanduser; "
    "Windows expanduser uses USERPROFILE/HOMEDRIVE+HOMEPATH instead of pwent",
)
def test_unexpandable_home_raises_actionable_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """When ``$HOME`` is unset and pwent lookup fails, ``Path.expanduser()`` on
    Python 3.12+ raises ``RuntimeError("Could not determine home directory.")``
    — a correct fail-fast but with no hint at how to fix it. Wrap the error
    in a message that names the env vars the operator can set."""
    import pwd

    monkeypatch.delenv("MEMTOMEM_FASTEMBED_CACHE", raising=False)
    monkeypatch.delenv("FASTEMBED_CACHE_PATH", raising=False)
    monkeypatch.delenv("HOME", raising=False)

    def _no_pwent(_uid: int) -> object:
        raise KeyError("simulated missing pwent")

    monkeypatch.setattr(pwd, "getpwuid", _no_pwent)
    # Precondition: bare expanduser surfaces the unhelpful Python error.
    with pytest.raises(RuntimeError, match="Could not determine home directory"):
        Path("~/foo").expanduser()

    with pytest.raises(RuntimeError, match="MEMTOMEM_FASTEMBED_CACHE") as excinfo:
        resolve_fastembed_cache_dir()
    # Original Python error is preserved as __cause__ for debugging.
    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert not (Path.cwd() / "~").exists(), "wrap must happen before mkdir"


posix_modes = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only: a Windows directory has an ACL, not a mode"
)


@pytest.fixture
def umask_022() -> Iterator[None]:
    """The usual umask, so a directory created without a mode comes out 0755."""
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _use_default(monkeypatch: pytest.MonkeyPatch, home: Path) -> Path:
    monkeypatch.delenv("MEMTOMEM_FASTEMBED_CACHE", raising=False)
    monkeypatch.delenv("FASTEMBED_CACHE_PATH", raising=False)
    set_home(monkeypatch, home)
    return home / ".memtomem" / "cache" / "fastembed"


@posix_modes
def test_default_dir_is_created_owner_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, umask_022: None
) -> None:
    """Another local account must not be able to traverse the default cache:
    huggingface-hub keeps its lock files in ``.locks`` under it."""
    default = _use_default(monkeypatch, tmp_path)

    resolved = resolve_fastembed_cache_dir()

    assert resolved == default
    assert _mode(default) == 0o700
    # Only the leaf is tightened; the parents hold other memtomem state.
    assert _mode(default.parent) == 0o755
    assert _mode(default.parent.parent) == 0o755
    # A second call finds the directory and leaves it as it is.
    assert resolve_fastembed_cache_dir() == default
    assert _mode(default) == 0o700


@posix_modes
def test_existing_default_dir_keeps_its_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, umask_022: None
) -> None:
    """Tightening a cache that already holds models would change who can read
    them, so an existing directory is left alone."""
    default = _use_default(monkeypatch, tmp_path)
    default.mkdir(parents=True)
    os.chmod(default, 0o755)

    assert resolve_fastembed_cache_dir() == default
    assert _mode(default) == 0o755


@posix_modes
@pytest.mark.parametrize("env", ["MEMTOMEM_FASTEMBED_CACHE", "FASTEMBED_CACHE_PATH"])
@pytest.mark.parametrize("names_default_location", [False, True])
def test_env_chosen_dir_is_created_with_the_umask_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    umask_022: None,
    env: str,
    names_default_location: bool,
) -> None:
    """A path the user chose may be a shared cache on purpose, so it is not
    tightened, even when it spells out the default location."""
    default = _use_default(monkeypatch, tmp_path)
    chosen = default if names_default_location else tmp_path / "shared-cache"
    monkeypatch.setenv(env, str(chosen))

    assert resolve_fastembed_cache_dir() == chosen
    assert _mode(chosen) == 0o755


def test_file_at_default_path_is_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    default = _use_default(monkeypatch, tmp_path)
    default.parent.mkdir(parents=True)
    default.write_text("not a directory")

    with pytest.raises(FileExistsError):
        resolve_fastembed_cache_dir()


def test_existing_default_dir_survives_a_non_eexist_mkdir_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``mkdir`` on an existing directory can report EACCES or EROFS instead of
    EEXIST (a read-only mount). The cache is still usable for reading, so the
    resolver returns it."""
    default = _use_default(monkeypatch, tmp_path)
    default.mkdir(parents=True)
    real_mkdir = os.mkdir

    def _mkdir(path: object, *args: object, **kwargs: object) -> None:
        if Path(os.fspath(path)) == default:  # type: ignore[arg-type]
            raise PermissionError(13, "Permission denied", str(default))
        real_mkdir(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "mkdir", _mkdir)

    assert resolve_fastembed_cache_dir() == default


def test_default_dir_mode_requested_follows_the_platform(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The tests that read a mode back are skipped on Windows, so this one
    checks the mode the resolver asks for: 0700 on POSIX, and on Windows the
    default it always passed."""
    default = _use_default(monkeypatch, tmp_path)
    requested: dict[Path, int] = {}
    real_mkdir = Path.mkdir

    def _mkdir(
        self: Path, mode: int = 0o777, parents: bool = False, exist_ok: bool = False
    ) -> None:
        requested.setdefault(self, mode)
        real_mkdir(self, mode=mode, parents=parents, exist_ok=exist_ok)

    monkeypatch.setattr(Path, "mkdir", _mkdir)

    assert resolve_fastembed_cache_dir() == default
    assert requested[default] == (0o777 if sys.platform == "win32" else 0o700)
