"""Delta saves use the file generation captured under the write lock (#2437)."""

from __future__ import annotations

import json
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

import pytest

from memtomem import config as config_module
from memtomem import config_signature
from memtomem.config import assign_section_fields, save_config_overrides
from memtomem.config_signature import build_fresh_config

from .test_config_profile_persistence import BGE, E5, home as home, write_config


def before_save_lock(monkeypatch, change) -> None:
    """Let a competing, cooperative writer finish immediately before our lock."""
    real_lock = config_module._config_write_lock

    @contextmanager
    def interleave(path, **kwargs):
        with real_lock(path):
            change()
        with real_lock(path, **kwargs):
            yield

    monkeypatch.setattr(config_module, "_config_write_lock", interleave)


@pytest.mark.parametrize("source", ["override", "fragment"])
@pytest.mark.parametrize(
    "initial,replacement,requested,expected_pin,expected_loaded",
    [
        (E5, BGE, 384, 384, 384),
        (BGE, E5, 384, None, 384),
        (E5, BGE, 512, None, 512),
        (BGE, E5, 512, 512, 512),
        (E5, BGE, None, None, 512),
        (BGE, E5, None, None, 384),
    ],
)
def test_model_switch_before_lock_uses_new_profile(
    home: Path, monkeypatch, source, initial, replacement, requested, expected_pin, expected_loaded
) -> None:
    path = write_config(home, {"indexing": {"auto_discover": False}, "custom": {"keep": 1}})
    model_path = path if source == "override" else path.parent / "config.d" / "10-model.json"
    initial_data = json.loads(path.read_text()) if source == "override" else {}
    model_path.write_text(json.dumps({**initial_data, "embedding": initial}), encoding="utf-8")
    cfg = build_fresh_config(migrate=False)
    if requested is not None:
        assign_section_fields(cfg.indexing, {"max_chunk_tokens": requested})
    cfg.search.default_top_k = 17
    original = cfg.model_dump()
    sections = {name: getattr(cfg, name) for name in type(cfg).model_fields}
    fields_set = {
        name: value.model_fields_set.copy()
        for name, value in sections.items()
        if hasattr(value, "model_fields_set")
    }

    def switch():
        model_path.write_text(
            json.dumps({**initial_data, "embedding": replacement}), encoding="utf-8"
        )

    before_save_lock(monkeypatch, switch)
    receipt = save_config_overrides(cfg)
    saved = json.loads(path.read_text())
    assert saved["indexing"].get("max_chunk_tokens") == expected_pin
    assert saved["indexing"] == (
        {"auto_discover": False}
        if expected_pin is None
        else {"auto_discover": False, "max_chunk_tokens": expected_pin}
    )
    assert saved["custom"] == {"keep": 1}
    assert saved["search"] == {"default_top_k": 17}
    assert receipt.after == saved
    if source == "override":
        assert receipt.before["embedding"] == replacement
        assert saved["embedding"] == replacement
    else:
        assert "embedding" not in saved
    fresh = build_fresh_config(migrate=False)
    assert fresh.embedding.model == replacement["model"]
    assert fresh.indexing.max_chunk_tokens == expected_loaded
    assert cfg.model_dump() == original
    assert all(getattr(cfg, name) is value for name, value in sections.items())
    assert all(getattr(cfg, name).model_fields_set == value for name, value in fields_set.items())


@pytest.mark.parametrize(
    "initial,replacement,requested,expected_pin",
    [(9, 19, 9, 9), (19, 9, 9, None), (None, 19, 5, 5), (19, None, 19, 19)],
    ids=["edit-keeps-pin", "edit-prunes-pin", "add", "remove"],
)
def test_fragment_change_before_lock_is_used(
    home: Path, monkeypatch, initial, replacement, requested, expected_pin
) -> None:
    path = write_config(home, {"indexing": {"auto_discover": False}})
    fragment = path.parent / "config.d" / "10-search.json"

    def replace(value):
        if value is None:
            fragment.unlink(missing_ok=True)
        else:
            fragment.write_text(json.dumps({"search": {"default_top_k": value}}), encoding="utf-8")

    replace(initial)
    cfg = build_fresh_config(migrate=False)
    cfg.search.default_top_k = requested
    before_save_lock(monkeypatch, lambda: replace(replacement))
    receipt = save_config_overrides(cfg)
    saved = json.loads(path.read_text())
    assert saved.get("search", {}).get("default_top_k") == expected_pin
    assert receipt.after == saved
    assert build_fresh_config(migrate=False).search.default_top_k == requested


def test_save_reads_each_file_once_under_lock(home: Path, monkeypatch) -> None:
    path = write_config(home, {"embedding": E5, "indexing": {"auto_discover": False}})
    fragment = path.parent / "config.d" / "10-budget.json"
    fragment.write_text(json.dumps({"indexing": {"max_chunk_tokens": 320}}), encoding="utf-8")
    cfg = build_fresh_config(migrate=False)
    cfg.search.default_top_k = 17
    calls: Counter[Path] = Counter()
    real_read = Path.read_text
    real_lock = config_module._config_write_lock
    locked = False

    @contextmanager
    def record_lock(config_path, **kwargs):
        nonlocal locked
        with real_lock(config_path, **kwargs):
            locked = True
            try:
                yield
            finally:
                locked = False

    def changing_read(file, *args, **kwargs):
        if file in (path, fragment):
            assert locked, "comparison inputs must be captured under the save lock"
            calls[file] += 1
            if calls[file] > 1:
                return json.dumps({"embedding": BGE, "indexing": {"max_chunk_tokens": 448}})
        return real_read(file, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(config_module, "_config_write_lock", record_lock)
        patch.setattr(Path, "read_text", changing_read)
        receipt = save_config_overrides(cfg)
    assert calls == {path: 1, fragment: 1}
    assert receipt.after == {
        "embedding": E5,
        "indexing": {"auto_discover": False},
        "search": {"default_top_k": 17},
    }
    assert json.loads(path.read_text()) == receipt.after
    assert build_fresh_config(migrate=False).indexing.max_chunk_tokens == 320


def test_empty_fragment_snapshot_does_not_read_new_fragments(home: Path, monkeypatch) -> None:
    path = write_config(home, {"embedding": E5})
    snapshot = config_module._read_config_file(path)
    fragment = path.parent / "config.d" / "10-search.json"
    fragment.write_text(json.dumps({"search": {"default_top_k": 19}}), encoding="utf-8")

    def unexpected_read(*args, **kwargs):
        pytest.fail("a supplied snapshot must not trigger another filesystem read")

    monkeypatch.setattr(Path, "read_text", unexpected_read)
    cfg = config_signature._build_config(
        _override_snapshot=snapshot, _fragment_snapshots=(), validate_profile=False
    )
    assert cfg.embedding.model == E5["model"]
    assert cfg.search.default_top_k == config_module.Mem2MemConfig().search.default_top_k


@pytest.mark.parametrize("fail_on", [1, 2], ids=["identity", "comparand"])
def test_baseline_failure_preserves_file_and_live_config(home: Path, monkeypatch, fail_on) -> None:
    path = write_config(home, {"embedding": E5, "indexing": {"auto_discover": False}})
    cfg = build_fresh_config(migrate=False)
    cfg.search.default_top_k = 17
    original = cfg.model_dump()
    original_file = (path.read_bytes(), path.stat().st_mtime_ns)
    real_build = config_signature._build_config
    builds = 0

    def fail_build(*args, **kwargs):
        nonlocal builds
        builds += 1
        if builds == fail_on:
            raise ValueError("baseline unavailable")
        return real_build(*args, **kwargs)

    monkeypatch.setattr(config_signature, "_build_config", fail_build)
    with pytest.raises(ValueError, match="baseline unavailable"):
        save_config_overrides(cfg)
    assert (path.read_bytes(), path.stat().st_mtime_ns) == original_file
    assert cfg.model_dump() == original
