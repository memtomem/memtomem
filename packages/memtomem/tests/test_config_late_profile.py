"""Later profile selection must not discard an earlier valid section (#2439)."""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import warnings
from collections import Counter
from pathlib import Path

import pytest

from memtomem import config as config_module
from memtomem.config import Mem2MemConfig, build_comparand, load_config_d, load_config_overrides
from memtomem.config_signature import build_fresh_config
from memtomem.errors import ConfigFragmentError
from _subprocess_env import subprocess_env

from .test_config_profile_persistence import BGE, E5, home as home, write_config


def fragment(home: Path, name: str, data: dict) -> Path:
    path = home / ".memtomem" / "config.d" / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.mark.parametrize("source", ["override", "later-fragment", "same-fragment"])
@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("migrate", [False, True])
def test_late_profile_preserves_the_whole_section(
    home: Path, monkeypatch: pytest.MonkeyPatch, source: str, strict: bool, migrate: bool
) -> None:
    budget = {"indexing": {"max_chunk_tokens": 320, "auto_discover": False}}
    budget_path = fragment(home, "20-budget.json", budget)
    override = write_config(home, {"embedding": E5} if source == "override" else {})
    if source == "later-fragment":
        fragment(home, "30-model.json", {"embedding": E5})
    elif source == "same-fragment":
        fragment(home, "20-budget.json", {**budget, "embedding": E5})
    before = [(p, p.read_bytes(), p.stat().st_mtime_ns) for p in (budget_path, override)]

    def unexpected_discovery():
        pytest.fail("auto_discover=False must prevent provider discovery")

    monkeypatch.setattr(config_module, "_canonical_provider_dirs", unexpected_discovery)
    cfg = build_fresh_config(migrate=migrate, strict_fragments=strict, strict_overrides=strict)
    assert (cfg.indexing.max_chunk_tokens, cfg.indexing.target_chunk_tokens) == (320, 320)
    assert cfg.indexing.auto_discover is False
    assert cfg.load_diagnostics == ()
    assert {"max_chunk_tokens", "auto_discover"} <= cfg.indexing.model_fields_set
    assert "target_chunk_tokens" not in cfg.indexing.model_fields_set
    assert "chunk_tokenizer_path" not in cfg.indexing.model_fields_set
    for path, content, mtime in before:
        assert (path.read_bytes(), path.stat().st_mtime_ns) == (content, mtime)
    assert not (override.parent / ".config.json.lock").exists()


@pytest.mark.parametrize("selected", [E5, BGE], ids=["e5", "generic"])
@pytest.mark.parametrize("strict", [False, True])
def test_invalid_section_retains_rollback_and_strictness(home: Path, selected: dict, strict: bool):
    path = fragment(
        home, "20-budget.json", {"indexing": {"max_chunk_tokens": 64, "auto_discover": False}}
    )
    write_config(home, {"embedding": selected})
    if strict:
        with pytest.raises(ConfigFragmentError, match=r"Invalid config section \[indexing\]"):
            build_fresh_config(migrate=False, strict_fragments=True)
        return
    cfg = build_fresh_config(migrate=False)
    assert cfg.indexing.auto_discover is True
    assert cfg.indexing.max_chunk_tokens == (384 if selected == E5 else 512)
    assert [(d.section, d.path, d.layer) for d in cfg.load_diagnostics] == [
        ("indexing", str(path), "config.d")
    ]
    assert "max_chunk_tokens" not in cfg.indexing.model_fields_set


def test_rejected_embedding_is_not_resurrected_from_a_later_state(home: Path) -> None:
    # The first fragment is invalid with the original fp32 default. Replaying
    # it from the later int8 state would accept BGE and mispredict the profile.
    artifact = str(home / "artifact")
    rejected = fragment(
        home, "10-rejected.json", {"embedding": {**BGE, "onnx_artifact_path": artifact}}
    )
    fragment(
        home, "20-budget.json", {"indexing": {"max_chunk_tokens": 320, "auto_discover": False}}
    )
    fragment(
        home,
        "30-model.json",
        {
            "embedding": {
                "provider": "onnx",
                "onnx_variant": "int8-arm64",
                "onnx_artifact_path": artifact,
            }
        },
    )
    write_config(home, {})
    cfg = build_fresh_config(migrate=False)
    assert cfg.embedding.model == "multilingual-e5-small"
    assert cfg.indexing.max_chunk_tokens == 320
    assert cfg.indexing.auto_discover is False
    assert [(d.section, d.path) for d in cfg.load_diagnostics] == [("embedding", str(rejected))]


def test_comparand_excludes_override_reads_and_uses_explicit_context(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    override = write_config(home, {"embedding": E5})
    fragment(
        home, "20-budget.json", {"indexing": {"max_chunk_tokens": 320, "auto_discover": False}}
    )
    context = config_module.EmbeddingConfig(**E5)
    read_text = Path.read_text

    def guarded_read(path: Path, *args, **kwargs):
        assert path != override, "comparands must not read config.json"
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read)
    generic = build_comparand()
    assert generic.embedding.provider == "none"
    assert generic.indexing.max_chunk_tokens == 512
    assert [d.section for d in generic.load_diagnostics] == ["indexing"]
    selected = build_comparand(embedding_context=context)
    assert selected.indexing.max_chunk_tokens == 320
    assert selected.indexing.auto_discover is False
    assert selected.load_diagnostics == ()
    assert "target_chunk_tokens" not in selected.indexing.model_fields_set


@pytest.mark.parametrize("strict", [False, True])
def test_profile_and_load_share_one_read_per_file(
    home: Path, monkeypatch: pytest.MonkeyPatch, strict: bool
) -> None:
    budget = fragment(
        home, "20-budget.json", {"indexing": {"max_chunk_tokens": 320, "auto_discover": False}}
    )
    override = write_config(home, {"embedding": E5})
    calls: Counter[Path] = Counter()
    read_text = Path.read_text

    def changing_read(path: Path, *args, **kwargs):
        if path in (budget, override):
            calls[path] += 1
            if calls[path] > 1:
                return json.dumps({"embedding": BGE, "indexing": {"auto_discover": True}})
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", changing_read)
    cfg = build_fresh_config(migrate=False, strict_overrides=strict)
    assert calls == {budget: 1, override: 1}
    assert cfg.embedding.model == E5["model"]
    assert cfg.indexing.max_chunk_tokens == 320
    assert cfg.indexing.auto_discover is False


@pytest.mark.parametrize("quiet", [False, True])
def test_projection_does_not_duplicate_logs_or_diagnostics(
    home: Path, caplog: pytest.LogCaptureFixture, quiet: bool
) -> None:
    fragment(home, "10-rejected.json", {"embedding": {**E5, "dimension": 1024}})
    fragment(home, "20-budget.json", {"indexing": {"max_chunk_tokens": 320}})
    write_config(home, {"embedding": E5})
    with caplog.at_level(logging.WARNING, logger="memtomem.config"):
        cfg = build_fresh_config(migrate=False, quiet=quiet)
    assert [d.section for d in cfg.load_diagnostics] == ["embedding"]
    rejected = [r for r in caplog.records if "Invalid config section" in r.message]
    assert len(rejected) == (0 if quiet else 1)


def test_direct_fragment_loader_keeps_its_existing_context(home: Path) -> None:
    fragment(home, "20-budget.json", {"indexing": {"max_chunk_tokens": 320}})
    write_config(home, {"embedding": E5})
    cfg = Mem2MemConfig()
    load_config_d(cfg, quiet=True)
    assert cfg.indexing.max_chunk_tokens == 512
    assert [d.section for d in cfg.load_diagnostics] == ["indexing"]


@pytest.mark.parametrize("spelling", ["section", "field"])
@pytest.mark.parametrize("selected", [E5, BGE], ids=["e5", "generic"])
def test_final_profile_respects_both_environment_spellings(
    home: Path, monkeypatch: pytest.MonkeyPatch, spelling: str, selected: dict
) -> None:
    fragment(home, "20-budget.json", {"indexing": {"max_chunk_tokens": 320}})
    write_config(home, {"embedding": E5})
    if spelling == "section":
        monkeypatch.setenv("MEMTOMEM_EMBEDDING", json.dumps(selected))
    else:
        monkeypatch.setenv("MEMTOMEM_EMBEDDING__MODEL", selected["model"])
    cfg = build_fresh_config(migrate=False)
    assert cfg.embedding.model == selected["model"]
    assert cfg.indexing.max_chunk_tokens == (320 if selected == E5 else 512)
    assert [d.section for d in cfg.load_diagnostics] == ([] if selected == E5 else ["indexing"])


@pytest.mark.parametrize("quiet", [False, True])
def test_projection_stays_silent_after_server_logging_setup(home: Path, quiet: bool) -> None:
    fragment(home, "10-rejected.json", {"embedding": {**E5, "dimension": 1024}})
    fragment(home, "20-budget.json", {"indexing": {"max_chunk_tokens": 320}})
    write_config(home, {"embedding": E5})
    # dictConfig changes process-global handlers; exercise the actual setup
    # in a child so it cannot disturb pytest or another test's logging state.
    env = subprocess_env(
        HOME=str(home),
        USERPROFILE=str(home),
        MEMTOMEM_LOG_FORMAT="json",
    )
    code = f"""
from memtomem import config
config._load_dotenv = lambda: None
from memtomem.config_signature import build_fresh_config
from memtomem.server.lifespan import _setup_logging
_setup_logging()
cfg = build_fresh_config(migrate=False, quiet={quiet!r})
assert cfg.indexing.max_chunk_tokens == 320
assert [d.section for d in cfg.load_diagnostics] == ['embedding']
"""
    result = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr.count("Invalid config section [embedding]") == (0 if quiet else 1)


def test_projection_does_not_duplicate_serialization_warnings(home: Path) -> None:
    write_config(home, {"embedding": {"dimension": "abc"}})
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        cfg = build_fresh_config(migrate=False, strict_overrides=False)
    assert [d.section for d in cfg.load_diagnostics] == ["embedding"]
    with warnings.catch_warnings(record=True) as direct_warnings:
        warnings.simplefilter("always")
        load_config_overrides(Mem2MemConfig(), migrate=False)
    assert [(w.category, str(w.message)) for w in caught] == [
        (w.category, str(w.message)) for w in direct_warnings
    ]


@pytest.mark.parametrize("layer", ["fragment", "override"])
@pytest.mark.parametrize("invalid", ["json", "object", "io"])
@pytest.mark.parametrize("strict", [False, True])
def test_captured_file_errors_keep_loader_policy(
    home: Path, monkeypatch: pytest.MonkeyPatch, layer: str, invalid: str, strict: bool
) -> None:
    path = fragment(home, "20-bad.json", {}) if layer == "fragment" else write_config(home, {})
    path.write_text("{" if invalid == "json" else "[]", encoding="utf-8")
    read_text = Path.read_text

    def read(path_to_read: Path, *args, **kwargs):
        if invalid == "io" and path_to_read == path:
            raise PermissionError("unreadable fixture")
        return read_text(path_to_read, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    kwargs = {"migrate": False, "strict_fragments": strict, "strict_overrides": strict}
    if strict:
        error = ConfigFragmentError if layer == "fragment" else (OSError, ValueError)
        with pytest.raises(error):
            build_fresh_config(**kwargs)
    else:
        cfg = build_fresh_config(**kwargs)
        assert cfg.embedding.provider == "none"
        assert cfg.indexing.max_chunk_tokens == 512


def test_rejected_final_embedding_cannot_rescue_a_fragment(home: Path) -> None:
    budget = fragment(home, "20-budget.json", {"indexing": {"max_chunk_tokens": 320}})
    override = write_config(home, {"embedding": {**E5, "dimension": 1024}})
    cfg = build_fresh_config(migrate=False, strict_overrides=False)
    assert cfg.embedding.provider == "none"
    assert cfg.indexing.max_chunk_tokens == 512
    assert [(d.section, d.path) for d in cfg.load_diagnostics] == [
        ("indexing", str(budget)),
        ("embedding", str(override)),
    ]


@pytest.mark.parametrize("custom", [False, True])
def test_registration_preserves_late_profile_fragment_roots(home: Path, monkeypatch, custom: bool):
    old = home / "old-project" / ".memtomem" / "memories"
    new = home / "new-project" / ".memtomem" / "memories.local"
    fragment(
        home,
        "20-budget.json",
        {
            "indexing": {
                "max_chunk_tokens": 320,
                "auto_discover": False,
                "project_memory_dirs": [str(old)],
            }
        },
    )
    default = write_config(home, {"embedding": BGE if custom else E5})
    target = home / "custom.json" if custom else default
    target.write_text(json.dumps({"embedding": E5}), encoding="utf-8")
    before = (target.read_bytes(), target.stat().st_mtime_ns)
    read_text = Path.read_text

    def read(path: Path, *args, **kwargs):
        if custom and path == default:
            pytest.fail("registration must use the explicit config_path, not the default file")
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    kwargs = {"config_path": target} if custom else {}
    assert config_module.register_project_memory_dir(old, **kwargs) is False
    assert (target.read_bytes(), target.stat().st_mtime_ns) == before
    assert config_module.register_project_memory_dir(new, **kwargs) is True
    saved = json.loads(target.read_text())
    roots = {
        Path(value).expanduser().resolve() for value in saved["indexing"]["project_memory_dirs"]
    }
    assert roots == {old.resolve(), new.resolve()}
    assert saved["embedding"] == E5
    assert "max_chunk_tokens" not in saved["indexing"]
    if not custom:
        cfg = build_fresh_config(migrate=False)
        assert {p.expanduser().resolve() for p in cfg.indexing.project_memory_dirs} == roots
        assert cfg.indexing.max_chunk_tokens == 320
        assert cfg.indexing.auto_discover is False


def test_registration_uses_the_profile_read_under_the_write_lock(home: Path, monkeypatch):
    from contextlib import contextmanager

    old = home / "old-project" / ".memtomem" / "memories"
    new = home / "new-project" / ".memtomem" / "memories"
    fragment(
        home,
        "20-budget.json",
        {"indexing": {"max_chunk_tokens": 320, "project_memory_dirs": [str(old)]}},
    )
    target = write_config(home, {"embedding": BGE})
    original_lock = config_module._config_write_lock

    @contextmanager
    def changing_lock(path):
        with original_lock(path):
            path.write_text(json.dumps({"embedding": E5}), encoding="utf-8")
            yield

    monkeypatch.setattr(config_module, "_config_write_lock", changing_lock)
    assert config_module.register_project_memory_dir(new) is True
    saved = json.loads(target.read_text())
    assert {Path(p).expanduser().resolve() for p in saved["indexing"]["project_memory_dirs"]} == {
        old.resolve(),
        new.resolve(),
    }


def test_registration_keeps_an_explicit_root_list_authoritative(home: Path):
    fragment_root = home / "fragment" / ".memtomem" / "memories"
    pinned_root = home / "pinned" / ".memtomem" / "memories"
    new = home / "new" / ".memtomem" / "memories"
    fragment(
        home,
        "20-budget.json",
        {"indexing": {"max_chunk_tokens": 320, "project_memory_dirs": [str(fragment_root)]}},
    )
    target = write_config(
        home, {"embedding": E5, "indexing": {"project_memory_dirs": [str(pinned_root)]}}
    )
    assert config_module.register_project_memory_dir(new) is True
    saved = json.loads(target.read_text())
    assert {Path(p).expanduser().resolve() for p in saved["indexing"]["project_memory_dirs"]} == {
        pinned_root.resolve(),
        new.resolve(),
    }
