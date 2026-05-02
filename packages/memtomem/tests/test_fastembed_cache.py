"""Tests for ``embedding._fastembed_cache.resolve_fastembed_cache_dir``.

The helper exists to keep the fastembed model snapshot out of macOS's
periodically-reaped ``/var/folders/.../T/`` tempdir (see the module
docstring for the failure mode). These tests pin the precedence rules and
the directory-creation contract; they do not exercise fastembed itself.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from memtomem.embedding._fastembed_cache import resolve_fastembed_cache_dir


def test_default_is_under_memtomem_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("MEMTOMEM_FASTEMBED_CACHE", raising=False)
    monkeypatch.delenv("FASTEMBED_CACHE_PATH", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

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
    monkeypatch.setenv("HOME", str(tmp_path))

    resolved = resolve_fastembed_cache_dir()

    assert resolved == tmp_path / ".memtomem" / "cache" / "fastembed"


def test_tilde_in_env_is_expanded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MEMTOMEM_FASTEMBED_CACHE", "~/custom-cache")
    monkeypatch.delenv("FASTEMBED_CACHE_PATH", raising=False)

    resolved = resolve_fastembed_cache_dir()

    assert resolved == tmp_path / "custom-cache"
    assert resolved.is_dir()
