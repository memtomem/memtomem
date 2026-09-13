"""Profile defaults must survive CLI, delta saves and Web resets (#2399)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from memtomem.cli import cli
from memtomem.cli.config_cmd import _effective_value
from memtomem.config import build_comparand, save_config_overrides
from memtomem.config_signature import build_fresh_config
from memtomem.web import hot_reload

from .helpers import set_home
from .test_web_hot_reload import app as app, client as client

E5 = {"provider": "onnx", "model": "intfloat/multilingual-e5-small"}
BGE = {"provider": "onnx", "model": "BAAI/bge-m3"}


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    set_home(monkeypatch, tmp_path)
    for name in list(os.environ):
        if name.upper().startswith("MEMTOMEM_"):
            monkeypatch.delenv(name)
    (tmp_path / ".memtomem" / "config.d").mkdir(parents=True)
    return tmp_path


def write_config(home: Path, data: dict) -> Path:
    path = home / ".memtomem" / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.fixture(params=["env", "fragment", "override"])
def profile(request, home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = write_config(home, {"indexing": {"auto_discover": False}})
    if request.param == "env":
        monkeypatch.setenv("MEMTOMEM_EMBEDDING", json.dumps(E5))
    elif request.param == "fragment":
        (home / ".memtomem" / "config.d" / "10-model.json").write_text(
            json.dumps({"embedding": E5}), encoding="utf-8"
        )
    else:
        write_config(home, {"embedding": E5, "indexing": {"auto_discover": False}})
    return path


@pytest.mark.parametrize("requested", [512, 384])
def test_cli_set_persists_only_the_profile_delta(profile: Path, requested: int) -> None:
    before = build_fresh_config(migrate=False)
    assert before.indexing.max_chunk_tokens == 384
    result = CliRunner().invoke(cli, ["config", "set", "indexing.max_chunk_tokens", str(requested)])
    assert result.exit_code == 0, result.output
    assert f"indexing.max_chunk_tokens: 384 -> {requested}" in result.output
    fresh = build_fresh_config(migrate=False)
    assert fresh.indexing.max_chunk_tokens == requested
    assert _effective_value("indexing", "max_chunk_tokens") == requested
    pins = json.loads(profile.read_text()).get("indexing", {})
    assert pins == (
        {"auto_discover": False, "max_chunk_tokens": 512}
        if requested == 512
        else {"auto_discover": False}
    )
    if requested == 384:
        assert "effective fallback" in result.output


def test_show_and_comparand_are_read_only(profile: Path) -> None:
    before = (profile.read_bytes(), profile.stat().st_mtime_ns)
    fresh = build_fresh_config(migrate=False)
    baseline = build_comparand(embedding_context=fresh.embedding)
    assert baseline.indexing.max_chunk_tokens == 384
    assert baseline.embedding.onnx_batch_size == 4
    assert "onnx_batch_size" not in baseline.embedding.model_fields_set
    assert "max_chunk_tokens" not in baseline.indexing.model_fields_set
    result = CliRunner().invoke(cli, ["config", "show", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["indexing"]["max_chunk_tokens"] == 384
    assert _effective_value("indexing", "max_chunk_tokens") == 384
    assert (profile.read_bytes(), profile.stat().st_mtime_ns) == before
    assert not (profile.parent / ".config.json.lock").exists()


def test_unrelated_save_prunes_profile_pins_and_model_switch_releases_defaults(home: Path) -> None:
    path = write_config(home, {"embedding": E5, "indexing": {"max_chunk_tokens": 384}})
    cfg = build_fresh_config(migrate=False)
    cfg.mmr.enabled = True
    receipt = save_config_overrides(cfg)
    assert receipt.pruned("indexing", "max_chunk_tokens")
    data = json.loads(path.read_text())
    assert "indexing" not in data
    assert data["embedding"] == E5
    data["embedding"] = BGE
    write_config(home, data)
    assert build_fresh_config(migrate=False).indexing.max_chunk_tokens == 512


@pytest.mark.parametrize("source", ["env", "fragment", "override"])
def test_editable_embedding_pin_is_not_its_own_baseline(
    home: Path, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    path = write_config(home, {"embedding": {**E5, "onnx_batch_size": 12}})
    if source == "env":
        monkeypatch.setenv("MEMTOMEM_EMBEDDING__ONNX_BATCH_SIZE", "12")
    elif source == "fragment":
        (path.parent / "config.d" / "10-batch.json").write_text(
            json.dumps({"embedding": {"onnx_batch_size": 12}}), encoding="utf-8"
        )
    cfg = build_fresh_config(migrate=False)
    context_before = (cfg.embedding.model_dump(), cfg.embedding.model_fields_set.copy())
    baseline = build_comparand(embedding_context=cfg.embedding)
    assert baseline.embedding.onnx_batch_size == (4 if source == "override" else 12)
    save_config_overrides(cfg)
    assert (cfg.embedding.model_dump(), cfg.embedding.model_fields_set) == context_before
    assert ("onnx_batch_size" in json.loads(path.read_text())["embedding"]) == (
        source == "override"
    )
    assert build_fresh_config(migrate=False).embedding.onnx_batch_size == 12


@pytest.mark.parametrize("lower,selected,expected", [(E5, BGE, 512), (BGE, E5, 384)])
def test_file_model_precedence_controls_profile_baseline(
    home: Path, lower: dict, selected: dict, expected: int
) -> None:
    path = write_config(home, {"embedding": selected})
    (path.parent / "config.d" / "10-model.json").write_text(
        json.dumps({"embedding": lower}), encoding="utf-8"
    )
    cfg = build_fresh_config(migrate=False)
    baseline = build_comparand(embedding_context=cfg.embedding)
    assert baseline.indexing.max_chunk_tokens == expected
    assert baseline.embedding.model == selected["model"]
    save_config_overrides(cfg)
    assert "indexing" not in json.loads(path.read_text())


def test_context_free_comparand_still_excludes_config_json(home: Path) -> None:
    write_config(home, {"embedding": E5, "indexing": {"project_memory_dirs": ["~/project"]}})
    baseline = build_comparand()
    assert baseline.embedding.provider == "none"
    assert baseline.indexing.max_chunk_tokens == 512
    assert baseline.indexing.project_memory_dirs == []


async def test_web_reset_then_save_removes_pin(home: Path, app, client) -> None:
    path = write_config(home, {"embedding": E5, "indexing": {"max_chunk_tokens": 512}})
    app.state.config = build_fresh_config(migrate=False)
    hot_reload.initialize_reload_state(app)
    app.state.config.search.default_top_k = 123
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    response = await client.get("/api/config/defaults")
    assert response.status_code == 200, response.text
    defaults = response.json()
    assert defaults["indexing"]["max_chunk_tokens"] == 384
    assert defaults["embedding"]["onnx_batch_size"] == 4
    assert defaults["search"]["default_top_k"] != 123
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    response = await client.patch(
        "/api/config?persist=true", json={"indexing": {"max_chunk_tokens": 384}}
    )
    assert response.status_code == 200, response.text
    assert "max_chunk_tokens" not in json.loads(path.read_text()).get("indexing", {})
    assert build_fresh_config(migrate=False).indexing.max_chunk_tokens == 384


@pytest.mark.parametrize("source", ["env", "section-env", "fragment"])
def test_explicit_lower_indexing_budget_remains_the_fallback(
    home: Path, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    path = write_config(home, {"embedding": E5, "indexing": {"max_chunk_tokens": 512}})
    if source == "env":
        monkeypatch.setenv("MEMTOMEM_INDEXING__MAX_CHUNK_TOKENS", "448")
    elif source == "section-env":
        monkeypatch.setenv("MEMTOMEM_INDEXING", json.dumps({"max_chunk_tokens": 448}))
    else:
        (path.parent / "config.d" / "10-budget.json").write_text(
            json.dumps({"indexing": {"max_chunk_tokens": 448}}), encoding="utf-8"
        )
    cfg = build_fresh_config(migrate=False)
    assert build_comparand(embedding_context=cfg.embedding).indexing.max_chunk_tokens == 448
    result = CliRunner().invoke(cli, ["config", "set", "indexing.max_chunk_tokens", "448"])
    assert result.exit_code == 0, result.output
    assert "max_chunk_tokens" not in json.loads(path.read_text()).get("indexing", {})
    assert build_fresh_config(migrate=False).indexing.max_chunk_tokens == 448


def test_environment_model_wins_over_file_profile(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_config(home, {"embedding": E5})
    monkeypatch.setenv("MEMTOMEM_EMBEDDING", json.dumps(BGE))
    cfg = build_fresh_config(migrate=False)
    baseline = build_comparand(embedding_context=cfg.embedding)
    assert cfg.embedding.model == baseline.embedding.model == BGE["model"]
    assert baseline.indexing.max_chunk_tokens == 512
    assert baseline.indexing.hard_max_chunk_tokens == 0


def test_quantized_profile_context_retains_tokenizer_without_pinning_budgets(home: Path) -> None:
    embedding = {**E5, "onnx_variant": "int8-arm64", "onnx_artifact_path": str(home / "model")}
    path = write_config(home, {"embedding": embedding})
    cfg = build_fresh_config(migrate=False)
    baseline = build_comparand(embedding_context=cfg.embedding)
    assert baseline.indexing.chunk_tokenizer_path == str(
        (home / "model" / "tokenizer.json").resolve()
    )
    assert baseline.indexing.model_dump() == cfg.indexing.model_dump()
    cfg.mmr.enabled = True
    save_config_overrides(cfg)
    assert json.loads(path.read_text()) == {"embedding": embedding, "mmr": {"enabled": True}}
