"""Tests for home-relative path serialization in config.json.

Pins the contract that writers serialize paths under ``$HOME`` as
``~/...`` while loaders continue to apply ``Path.expanduser()`` so the
round-trip is symmetric. Covers the feasibility goal documented in
``docs/guides/configuration.md`` "Moving config.json between machines"
section: a config written on one machine remains usable when copied
(or git-synced) to another with a different ``$HOME``.

Outside-``$HOME`` paths (``/var/log/...``, ``/opt/...``) stay absolute
because their meaning is genuinely machine-specific.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from memtomem import config as _cfg
from memtomem.config import (
    Mem2MemConfig,
    _portable_path_str,
    _relativize_config_paths_in_place,
    load_config_overrides,
    save_config_overrides,
)

from .helpers import set_home


# ---------------------------------------------------------------------------
# _portable_path_str — direct unit tests (pure function)
# ---------------------------------------------------------------------------


def test_path_under_home_becomes_tilde(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    set_home(monkeypatch, tmp_path)
    p = tmp_path / ".memtomem" / "memtomem.db"
    assert _portable_path_str(p) == "~/.memtomem/memtomem.db"


def test_path_outside_home_stays_absolute(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    set_home(monkeypatch, tmp_path)
    # Use a sibling tmp dir that is guaranteed not under HOME.
    elsewhere = tmp_path.parent / "elsewhere" / "data.db"
    elsewhere.parent.mkdir(parents=True, exist_ok=True)
    assert _portable_path_str(elsewhere) == str(elsewhere)


def test_already_tilde_input_passes_through() -> None:
    assert _portable_path_str("~/.memtomem/memtomem.db") == "~/.memtomem/memtomem.db"
    assert _portable_path_str("~") == "~"


def test_relative_path_unchanged() -> None:
    assert _portable_path_str("relative/sub/dir") == "relative/sub/dir"


def test_home_root_collapses_to_bare_tilde(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    set_home(monkeypatch, tmp_path)
    assert _portable_path_str(tmp_path) == "~"


def test_path_object_and_string_produce_same_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_home(monkeypatch, tmp_path)
    p = tmp_path / "memories"
    assert _portable_path_str(p) == _portable_path_str(str(p)) == "~/memories"


# ---------------------------------------------------------------------------
# _relativize_config_paths_in_place — schema-aware transform
# ---------------------------------------------------------------------------


def test_relativize_in_place_handles_known_scalar_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_home(monkeypatch, tmp_path)
    data: dict = {"storage": {"sqlite_path": str(tmp_path / "db.sqlite")}}
    _relativize_config_paths_in_place(data)
    assert data["storage"]["sqlite_path"] == "~/db.sqlite"


def test_relativize_in_place_handles_known_list_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_home(monkeypatch, tmp_path)
    data: dict = {
        "indexing": {
            "memory_dirs": [
                str(tmp_path / "memories"),
                str(tmp_path / "notes"),
            ],
        }
    }
    _relativize_config_paths_in_place(data)
    assert data["indexing"]["memory_dirs"] == ["~/memories", "~/notes"]


def test_relativize_in_place_accepts_path_objects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_home(monkeypatch, tmp_path)
    data: dict = {
        "storage": {"sqlite_path": tmp_path / "db.sqlite"},
        "indexing": {"memory_dirs": [tmp_path / "memories"]},
    }
    _relativize_config_paths_in_place(data)
    assert data["storage"]["sqlite_path"] == "~/db.sqlite"
    assert data["indexing"]["memory_dirs"] == ["~/memories"]


def test_relativize_in_place_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    set_home(monkeypatch, tmp_path)
    data: dict = {
        "storage": {"sqlite_path": "~/db.sqlite"},
        "indexing": {"memory_dirs": ["~/memories"]},
    }
    _relativize_config_paths_in_place(data)
    _relativize_config_paths_in_place(data)
    assert data["storage"]["sqlite_path"] == "~/db.sqlite"
    assert data["indexing"]["memory_dirs"] == ["~/memories"]


def test_relativize_in_place_missing_sections_no_error() -> None:
    data: dict = {"unrelated": {"foo": "bar"}}
    _relativize_config_paths_in_place(data)
    assert data == {"unrelated": {"foo": "bar"}}


def test_relativize_in_place_preserves_outside_home_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_home(monkeypatch, tmp_path)
    elsewhere = tmp_path.parent / "outside" / "db.sqlite"
    elsewhere.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {"storage": {"sqlite_path": str(elsewhere)}}
    _relativize_config_paths_in_place(data)
    assert data["storage"]["sqlite_path"] == str(elsewhere)


# ---------------------------------------------------------------------------
# save_config_overrides — end-to-end write produces tilde paths
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Redirect ``$HOME`` and the override path so saves land in tmp_path."""
    set_home(monkeypatch, tmp_path)
    override = tmp_path / "config.json"
    monkeypatch.setattr(_cfg, "_override_path", lambda: override)
    monkeypatch.setattr(_cfg, "_canonical_provider_dirs", lambda: [])
    return tmp_path, override


def test_save_writes_memory_dirs_as_tilde(
    isolated_config: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``indexing.memory_dirs`` participates in delta-only save and is the
    primary path-typed mutable field — exercises the ``save_config_overrides``
    write path end-to-end."""
    home, override = isolated_config
    cfg = Mem2MemConfig()
    cfg.indexing.memory_dirs = [home / "team-notes", home / "personal-notes"]
    save_config_overrides(cfg)
    raw = json.loads(override.read_text(encoding="utf-8"))
    assert raw["indexing"]["memory_dirs"] == ["~/team-notes", "~/personal-notes"]


def test_save_keeps_outside_home_memory_dirs_absolute(
    isolated_config: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    home, override = isolated_config
    elsewhere_dir = home.parent / "shared-volume"
    elsewhere_dir.mkdir(parents=True, exist_ok=True)
    cfg = Mem2MemConfig()
    cfg.indexing.memory_dirs = [elsewhere_dir / "team"]
    save_config_overrides(cfg)
    raw = json.loads(override.read_text(encoding="utf-8"))
    assert raw["indexing"]["memory_dirs"] == [str(elsewhere_dir / "team")]


def test_save_writes_sqlite_path_via_read_merge_write(
    isolated_config: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``storage.sqlite_path`` is init-only and not mutable through
    ``save_config_overrides`` directly — it lands in ``config.json``
    via the wizard, then survives subsequent saves through
    read-merge-write. This pins the relativize pass on the carry-over
    path so a wizard-written tilde stays a tilde and a wizard-written
    absolute gets rewritten on the next non-trivial save."""
    home, override = isolated_config
    # Simulate a wizard-written file with an absolute sqlite_path.
    pre_existing: dict = {
        "storage": {
            "backend": "sqlite",
            "sqlite_path": str(home / "wizard" / "memtomem.db"),
        },
    }
    override.write_text(json.dumps(pre_existing), encoding="utf-8")

    cfg = Mem2MemConfig()
    load_config_overrides(cfg)
    cfg.search.default_top_k = 17  # force a save through a mutable field
    save_config_overrides(cfg)

    raw = json.loads(override.read_text(encoding="utf-8"))
    assert raw["storage"]["sqlite_path"] == "~/wizard/memtomem.db"


# ---------------------------------------------------------------------------
# Round-trip: write tilde -> load resolves to absolute under current HOME
# ---------------------------------------------------------------------------


def test_round_trip_tilde_resolves_under_current_home(
    isolated_config: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Write tilde-form, load, expect callers' ``expanduser()`` to resolve
    under the current HOME.

    ``load_config_overrides`` does not auto-expand tildes (Pydantic
    ``setattr`` skips validators with ``validate_assignment=False``).
    Downstream code that uses the paths runs ``Path(d).expanduser()``
    explicitly — that's the contract the stored tilde form preserves
    across machines.
    """
    home, override = isolated_config
    cfg = Mem2MemConfig()
    cfg.indexing.memory_dirs = [home / "shared", home / "personal"]
    save_config_overrides(cfg)

    fresh = Mem2MemConfig()
    load_config_overrides(fresh)
    expanded = [Path(d).expanduser() for d in fresh.indexing.memory_dirs]
    assert expanded == [home / "shared", home / "personal"]


def test_cross_machine_simulation_different_home_resolves_correctly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Save under HOME-A; copy file; load under HOME-B; paths track HOME-B.

    Mimics the documented "moving config.json between machines" workflow:
    a `~/...` path written on machine A resolves to machine B's home when
    the config file is copied and reloaded there.
    """
    home_a = tmp_path / "home_a"
    home_a.mkdir()
    home_b = tmp_path / "home_b"
    home_b.mkdir()

    # Phase 1 — write config under HOME-A. Pre-seed an absolute
    # ``sqlite_path`` (init-only field) so the relativize pass on the
    # subsequent save converts it to tilde form alongside the mutable
    # ``memory_dirs`` write.
    set_home(monkeypatch, home_a)
    override_a = home_a / "config.json"
    monkeypatch.setattr(_cfg, "_override_path", lambda: override_a)
    monkeypatch.setattr(_cfg, "_canonical_provider_dirs", lambda: [])
    pre: dict = {"storage": {"sqlite_path": str(home_a / "memtomem.db")}}
    override_a.write_text(json.dumps(pre), encoding="utf-8")

    cfg = Mem2MemConfig()
    load_config_overrides(cfg)
    cfg.indexing.memory_dirs = [home_a / "memories"]
    save_config_overrides(cfg)

    # Confirm the on-disk file uses tilde form, not absolute.
    raw = json.loads(override_a.read_text(encoding="utf-8"))
    assert raw["storage"]["sqlite_path"] == "~/memtomem.db"
    assert raw["indexing"]["memory_dirs"] == ["~/memories"]

    # Copy to HOME-B (simulating git pull / machine migration).
    override_b = home_b / "config.json"
    override_b.write_bytes(override_a.read_bytes())

    # Phase 2 — load under HOME-B, expect the tilde to expand to home_b.
    set_home(monkeypatch, home_b)
    monkeypatch.setattr(_cfg, "_override_path", lambda: override_b)

    fresh = Mem2MemConfig()
    load_config_overrides(fresh)
    # Downstream code calls ``expanduser()`` per-path; the tilde-form on
    # disk lets that expansion track HOME-B even though the file came
    # from HOME-A.
    assert Path(fresh.storage.sqlite_path).expanduser() == home_b / "memtomem.db"
    expanded_dirs = [Path(d).expanduser() for d in fresh.indexing.memory_dirs]
    assert expanded_dirs == [home_b / "memories"]


# ---------------------------------------------------------------------------
# Backward compatibility: legacy absolute-path configs still load
# ---------------------------------------------------------------------------


def test_legacy_absolute_path_in_config_still_loads(
    isolated_config: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pre-existing config.json with absolute paths must load unchanged.

    Backward compat for users on the same machine the config was first
    written on. Phase 1 only changes the *writer*; the reader has always
    accepted absolute paths and continues to.
    """
    home, override = isolated_config
    legacy: dict = {
        "storage": {"sqlite_path": str(home / "legacy" / "memtomem.db")},
        "indexing": {
            "memory_dirs": [str(home / "legacy_dir1"), str(home / "legacy_dir2")],
        },
    }
    override.write_text(json.dumps(legacy), encoding="utf-8")

    fresh = Mem2MemConfig()
    load_config_overrides(fresh)
    assert Path(fresh.storage.sqlite_path).expanduser() == home / "legacy" / "memtomem.db"
    expanded_dirs = [Path(d).expanduser() for d in fresh.indexing.memory_dirs]
    assert expanded_dirs == [home / "legacy_dir1", home / "legacy_dir2"]


def test_legacy_absolute_rewrites_to_tilde_on_next_save(
    isolated_config: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A legacy absolute-path config gets converted to tilde on the next
    save — user opts into portability the moment they touch the config."""
    home, override = isolated_config
    legacy: dict = {
        "storage": {"sqlite_path": str(home / "legacy" / "memtomem.db")},
    }
    override.write_text(json.dumps(legacy), encoding="utf-8")

    cfg = Mem2MemConfig()
    load_config_overrides(cfg)
    # Touch a different field so save_config_overrides actually writes.
    cfg.search.default_top_k = 42
    save_config_overrides(cfg)

    raw = json.loads(override.read_text(encoding="utf-8"))
    # storage.sqlite_path is a non-mutable init-only field, so
    # save_config_overrides preserves it via read-merge-write. The
    # relativize pass rewrites it into tilde form on the way out.
    assert raw["storage"]["sqlite_path"] == "~/legacy/memtomem.db"
