"""Tests for config precedence: env vars win over ~/.memtomem/config.json.

Documents the invariant that ``MEMTOMEM_<SECTION>__<FIELD>`` env vars take
precedence over persisted overrides in ``~/.memtomem/config.json``. Matches
what every ``.mcp.json`` example in the docs assumes.

Regression anchor for issue #248.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from memtomem import config as _cfg
from memtomem.config import Mem2MemConfig, load_config_d, load_config_overrides


@pytest.fixture
def override_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the override file to tmp_path to avoid touching ~/.memtomem/."""
    p = tmp_path / "config.json"
    monkeypatch.setattr(_cfg, "_override_path", lambda: p)
    return p


@pytest.fixture
def config_d_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the config.d directory to tmp_path/config.d."""
    d = tmp_path / "config.d"
    d.mkdir()
    monkeypatch.setattr(_cfg, "_config_d_path", lambda: d)
    return d


def _clear_all_memtomem_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip any ambient MEMTOMEM_* so the test env is fully deterministic."""
    import os

    for name in list(os.environ):
        if name.startswith("MEMTOMEM_"):
            monkeypatch.delenv(name, raising=False)


def test_config_json_applies_when_no_env(
    override_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_all_memtomem_env(monkeypatch)
    override_path.write_text(
        json.dumps({"storage": {"sqlite_path": "/from/config.db"}}), encoding="utf-8"
    )
    cfg = Mem2MemConfig()
    load_config_overrides(cfg)
    assert str(cfg.storage.sqlite_path) == "/from/config.db"


def test_env_var_wins_over_config_json_scalar(
    override_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_all_memtomem_env(monkeypatch)
    monkeypatch.setenv("MEMTOMEM_STORAGE__SQLITE_PATH", "/from/env.db")
    override_path.write_text(
        json.dumps({"storage": {"sqlite_path": "/from/config.db"}}), encoding="utf-8"
    )
    cfg = Mem2MemConfig()
    load_config_overrides(cfg)
    assert str(cfg.storage.sqlite_path) == "/from/env.db"


def test_env_var_wins_over_config_json_list(
    override_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_all_memtomem_env(monkeypatch)
    monkeypatch.setenv("MEMTOMEM_INDEXING__MEMORY_DIRS", '["/from/env"]')
    override_path.write_text(
        json.dumps({"indexing": {"memory_dirs": ["/from/config"]}}), encoding="utf-8"
    )
    cfg = Mem2MemConfig()
    load_config_overrides(cfg)
    assert [str(p) for p in cfg.indexing.memory_dirs] == ["/from/env"]


def test_env_and_config_coexist_on_different_fields(
    override_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Env on one field, config.json on another — both should take effect."""
    _clear_all_memtomem_env(monkeypatch)
    monkeypatch.setenv("MEMTOMEM_STORAGE__SQLITE_PATH", "/from/env.db")
    override_path.write_text(
        json.dumps(
            {
                "storage": {"sqlite_path": "/from/config.db"},
                "embedding": {"model": "from-config"},
            }
        ),
        encoding="utf-8",
    )
    cfg = Mem2MemConfig()
    load_config_overrides(cfg)
    assert str(cfg.storage.sqlite_path) == "/from/env.db"
    assert cfg.embedding.model == "from-config"


def test_regression_pr247_mcp_json_env_block(
    override_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact ``.mcp.json`` env block shipped in docs (integrations/claude-code.md
    via PR #247) must work end-to-end after ``mm init`` has written a
    ``config.json``. One scalar (``SQLITE_PATH``), one list (``MEMORY_DIRS``).
    """
    _clear_all_memtomem_env(monkeypatch)
    monkeypatch.setenv("MEMTOMEM_STORAGE__SQLITE_PATH", "~/.memtomem/memtomem.db")
    monkeypatch.setenv("MEMTOMEM_INDEXING__MEMORY_DIRS", '["~/notes"]')
    override_path.write_text(
        json.dumps(
            {
                "storage": {"sqlite_path": "/tmp/wizard.db"},
                "indexing": {"memory_dirs": ["/tmp/wizard-memories"]},
            }
        ),
        encoding="utf-8",
    )
    cfg = Mem2MemConfig()
    load_config_overrides(cfg)
    assert str(cfg.storage.sqlite_path) == "~/.memtomem/memtomem.db"
    assert [str(p) for p in cfg.indexing.memory_dirs] == ["~/notes"]


# ---------------------------------------------------------------------------
# config.d fragment loader (Phase 2b)
# ---------------------------------------------------------------------------


def test_config_d_append_merges_with_defaults(
    config_d_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """APPEND list field: fragment entries added on top of existing list."""
    _clear_all_memtomem_env(monkeypatch)
    (config_d_dir / "claude-desktop.json").write_text(
        json.dumps({"indexing": {"memory_dirs": ["/from/fragment"]}}), encoding="utf-8"
    )
    cfg = Mem2MemConfig()
    before = list(cfg.indexing.memory_dirs)
    load_config_d(cfg)
    after = [str(p) for p in cfg.indexing.memory_dirs]
    assert "/from/fragment" in after
    for original in before:
        assert str(original) in after


def test_config_d_append_dedupes(
    config_d_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Duplicate APPEND entries across fragments collapse to one."""
    _clear_all_memtomem_env(monkeypatch)
    (config_d_dir / "01-a.json").write_text(
        json.dumps({"indexing": {"exclude_patterns": ["*.tmp", "*.log"]}}), encoding="utf-8"
    )
    (config_d_dir / "02-b.json").write_text(
        json.dumps({"indexing": {"exclude_patterns": ["*.log", "*.bak"]}}), encoding="utf-8"
    )
    cfg = Mem2MemConfig()
    load_config_d(cfg)
    assert cfg.indexing.exclude_patterns == ["*.tmp", "*.log", "*.bak"]


def test_config_d_replace_overwrites(
    config_d_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REPLACE list field: last fragment wins, prior list discarded."""
    _clear_all_memtomem_env(monkeypatch)
    (config_d_dir / "01-a.json").write_text(
        json.dumps({"search": {"rrf_weights": [0.5, 0.5]}}), encoding="utf-8"
    )
    (config_d_dir / "02-b.json").write_text(
        json.dumps({"search": {"rrf_weights": [0.3, 0.7]}}), encoding="utf-8"
    )
    cfg = Mem2MemConfig()
    load_config_d(cfg)
    assert cfg.search.rrf_weights == [0.3, 0.7]


def test_config_d_scalar_last_wins(
    config_d_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scalar field: last fragment applied wins."""
    _clear_all_memtomem_env(monkeypatch)
    (config_d_dir / "01-a.json").write_text(
        json.dumps({"storage": {"sqlite_path": "/a.db"}}), encoding="utf-8"
    )
    (config_d_dir / "02-b.json").write_text(
        json.dumps({"storage": {"sqlite_path": "/b.db"}}), encoding="utf-8"
    )
    cfg = Mem2MemConfig()
    load_config_d(cfg)
    assert str(cfg.storage.sqlite_path) == "/b.db"


def test_config_d_env_wins_over_fragments(
    config_d_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Env var set → fragment value for that field skipped."""
    _clear_all_memtomem_env(monkeypatch)
    monkeypatch.setenv("MEMTOMEM_STORAGE__SQLITE_PATH", "/from/env.db")
    (config_d_dir / "a.json").write_text(
        json.dumps({"storage": {"sqlite_path": "/from/fragment.db"}}), encoding="utf-8"
    )
    cfg = Mem2MemConfig()
    load_config_d(cfg)
    assert str(cfg.storage.sqlite_path) == "/from/env.db"


def test_config_d_unknown_section_warned_but_not_fatal(
    config_d_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Fragment with unknown section logs a warning and is skipped; valid
    fields in the same fragment still apply."""
    import logging

    _clear_all_memtomem_env(monkeypatch)
    (config_d_dir / "a.json").write_text(
        json.dumps(
            {"storage": {"sqlite_path": "/ok.db"}, "nope_not_a_section": {"x": 1}}
        ),
        encoding="utf-8",
    )
    cfg = Mem2MemConfig()
    with caplog.at_level(logging.WARNING, logger="memtomem.config"):
        load_config_d(cfg)
    assert str(cfg.storage.sqlite_path) == "/ok.db"
    assert any("nope_not_a_section" in r.message for r in caplog.records)


def test_config_d_invalid_json_warned(
    config_d_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Malformed JSON in a fragment is logged and skipped without killing startup."""
    import logging

    _clear_all_memtomem_env(monkeypatch)
    (config_d_dir / "bad.json").write_text("{ not valid json", encoding="utf-8")
    (config_d_dir / "good.json").write_text(
        json.dumps({"storage": {"sqlite_path": "/ok.db"}}), encoding="utf-8"
    )
    cfg = Mem2MemConfig()
    with caplog.at_level(logging.WARNING, logger="memtomem.config"):
        load_config_d(cfg)
    assert str(cfg.storage.sqlite_path) == "/ok.db"
    assert any("bad.json" in r.message for r in caplog.records)


def test_config_d_ignores_non_json_files(
    config_d_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only *.json files in config.d/ are read; README.md etc. are ignored."""
    _clear_all_memtomem_env(monkeypatch)
    (config_d_dir / "README.md").write_text("not a fragment", encoding="utf-8")
    (config_d_dir / "a.json").write_text(
        json.dumps({"storage": {"sqlite_path": "/ok.db"}}), encoding="utf-8"
    )
    cfg = Mem2MemConfig()
    load_config_d(cfg)
    assert str(cfg.storage.sqlite_path) == "/ok.db"


# ---------------------------------------------------------------------------
# Enforcement: every list[*] field must declare a merge strategy.
#
# Guards against future contributors adding a ``list[X]`` field to a config
# section without picking APPEND vs REPLACE. Without this test, the fragment
# loader would silently fall through to the REPLACE branch for unannotated
# list fields, letting a fragment clobber a positional list by accident.
# ---------------------------------------------------------------------------


def test_every_list_field_declares_merge_strategy() -> None:
    """Fails loudly if a new ``list[*]`` field in any ``Mem2MemConfig`` section
    is missing a ``MergeStrategy`` annotation.
    """
    from typing import get_origin

    from memtomem.config import MergeStrategy

    missing: list[str] = []
    for section_name, section_field in Mem2MemConfig.model_fields.items():
        sec_cls = section_field.annotation
        if not (isinstance(sec_cls, type) and hasattr(sec_cls, "model_fields")):
            continue
        for field_name, info in sec_cls.model_fields.items():
            if get_origin(info.annotation) is list:
                has_strategy = any(isinstance(m, MergeStrategy) for m in info.metadata)
                if not has_strategy:
                    missing.append(f"{section_name}.{field_name}")
    assert not missing, (
        "These list[*] fields lack a MergeStrategy annotation — wrap the "
        "type in Annotated[list[X], APPEND] or Annotated[list[X], REPLACE] "
        "in config.py:\n  - " + "\n  - ".join(missing)
    )
