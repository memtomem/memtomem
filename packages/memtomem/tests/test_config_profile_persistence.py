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


@pytest.fixture(
    params=[
        ("chunk_model_tokens", 1024, 512),
        ("chunk_input_prefix", "legacy: ", "passage: "),
        ("chunk_tokenizer_path", "old-tokenizer.json", None),
        ("chunk_context_tokens", 512, 96),
    ],
    ids=["model-budget", "prefix", "tokenizer", "context-budget"],
)
def repaired_profile(request, home: Path) -> tuple[Path, str, object]:
    from memtomem.embedding.profiles import E5_TOKENIZER

    field, previous, repaired = request.param
    if field == "chunk_tokenizer_path":
        repaired = E5_TOKENIZER
    path = write_config(home, {"embedding": E5, "indexing": {field: repaired}})
    (path.parent / "config.d" / "10-old-model.json").write_text(
        json.dumps({"embedding": BGE, "indexing": {field: previous}}), encoding="utf-8"
    )
    return path, field, previous


def test_save_accepts_a_profile_repaired_by_overrides(repaired_profile) -> None:
    path, field, previous = repaired_profile
    cfg = build_fresh_config(migrate=False)
    baseline = build_comparand(embedding_context=cfg.embedding)
    assert getattr(baseline.indexing, field) == previous
    cfg.mmr.enabled = True
    save_config_overrides(cfg)
    fresh = build_fresh_config(migrate=False)
    assert fresh.mmr.enabled
    assert fresh.indexing.model_dump() == cfg.indexing.model_dump()
    assert json.loads(path.read_text())["embedding"] == E5


async def test_web_defaults_and_save_accept_repaired_profile(repaired_profile, app, client) -> None:
    path, _, _ = repaired_profile
    app.state.config = build_fresh_config(migrate=False)
    hot_reload.initialize_reload_state(app)
    before = path.read_bytes()
    response = await client.get("/api/config/defaults")
    assert response.status_code == 200, response.text
    assert path.read_bytes() == before
    response = await client.patch("/api/config?persist=true", json={"mmr": {"enabled": True}})
    assert response.status_code == 200, response.text
    assert build_fresh_config(migrate=False).mmr.enabled


def test_project_registration_accepts_profile_repaired_by_overrides(home: Path) -> None:
    from memtomem.config import register_project_memory_dir

    path = write_config(home, {"indexing": {"chunk_model_tokens": 512}})
    (path.parent / "config.d" / "10-model.json").write_text(
        json.dumps({"embedding": E5, "indexing": {"chunk_model_tokens": 1024}}), encoding="utf-8"
    )
    assert build_fresh_config(migrate=False).indexing.chunk_model_tokens == 512
    target = home / "project" / ".memtomem" / "memories"
    assert register_project_memory_dir(target)
    assert [
        entry.expanduser().resolve()
        for entry in build_fresh_config(migrate=False).indexing.project_memory_dirs
    ] == [target.resolve()]
    assert not register_project_memory_dir(target)


def test_complete_load_still_rejects_incompatible_profile(home: Path) -> None:
    write_config(home, {"embedding": E5, "indexing": {"chunk_model_tokens": 1024}})
    with pytest.raises(ValueError, match="E5 requires exact chunk budgets"):
        build_fresh_config(migrate=False)


async def test_rejected_web_edit_preserves_generated_fields(home: Path, app, client) -> None:
    write_config(home, {"embedding": E5})
    cfg = app.state.config = build_fresh_config(migrate=False)
    hot_reload.initialize_reload_state(app)
    before = cfg.indexing.model_dump()
    fields_set = cfg.indexing.model_fields_set
    original_set = fields_set.copy()
    response = await client.patch(
        "/api/config?persist=true", json={"indexing": {"max_chunk_tokens": 64}}
    )
    assert response.status_code == 200, response.text
    assert response.json()["rejected"]
    assert cfg.indexing.model_dump() == before
    assert cfg.indexing.model_fields_set is fields_set
    assert fields_set == original_set
    response = await client.patch("/api/config?persist=true", json={"mmr": {"enabled": True}})
    assert response.status_code == 200, response.text
    assert build_fresh_config(migrate=False).mmr.enabled
    # The rejected edit must not prevent an unpinned budget from following
    # a subsequent profile change on this same object.
    from memtomem.config import EmbeddingConfig
    from memtomem.embedding.profiles import apply_e5_defaults

    cfg.embedding = EmbeddingConfig(**BGE)
    apply_e5_defaults(cfg)
    assert cfg.indexing.max_chunk_tokens == 512


@pytest.mark.parametrize(
    "updates",
    [{"max_chunk_tokens": 64}, {"max_chunk_tokens": 64, "target_chunk_tokens": 32}],
    ids=["single-field", "multiple-fields"],
)
def test_rejected_update_restores_existing_explicit_fields(home: Path, updates: dict) -> None:
    from memtomem.config import assign_section_fields

    write_config(home, {"embedding": E5, "indexing": {"chunk_overlap_tokens": 0}})
    cfg = build_fresh_config(migrate=False)
    before = cfg.indexing.model_dump()
    fields_set = cfg.indexing.model_fields_set
    assert fields_set == {"chunk_overlap_tokens"}
    with pytest.raises(ValueError):
        assign_section_fields(cfg.indexing, updates)
    assert cfg.indexing.model_dump() == before
    assert cfg.indexing.model_fields_set is fields_set
    assert fields_set == {"chunk_overlap_tokens"}
    assign_section_fields(cfg.indexing, {"max_chunk_tokens": 512})
    assert fields_set == {"chunk_overlap_tokens", "max_chunk_tokens"}


@pytest.fixture(
    params=[
        ("chunk_input_prefix", "legacy: "),
        ("chunk_model_tokens", 1024),
        ("chunk_tokenizer_path", "old-tokenizer.json"),
        ("chunk_context_tokens", 512),
    ],
    ids=["prefix", "model-budget", "tokenizer", "context-budget"],
)
def invalid_profile_data(request) -> tuple[str, object]:
    return request.param


@pytest.fixture
def invalid_profile(invalid_profile_data, home: Path) -> tuple[Path, str, object]:
    field, value = invalid_profile_data
    path = write_config(home, {"embedding": E5, "indexing": {field: value}})
    with pytest.raises(ValueError):
        build_fresh_config(migrate=False)
    return path, field, value


def test_show_can_inspect_a_profile_that_cannot_run(invalid_profile) -> None:
    path, field, value = invalid_profile
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    result = CliRunner().invoke(cli, ["config", "show", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["indexing"][field] == value
    assert _effective_value("indexing", field) == value
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_set_reports_profile_error_without_a_traceback(invalid_profile) -> None:
    path, _, _ = invalid_profile
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    entries = set(path.parent.iterdir())
    result = CliRunner().invoke(cli, ["config", "set", "mmr.enabled", "true"])
    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert "mmr.enabled was not saved" in result.output
    assert "Traceback" not in result.output
    assert "mmr" not in json.loads(path.read_text())
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert set(path.parent.iterdir()) == entries


async def test_web_defaults_stay_available_for_invalid_profile(
    client, home: Path, invalid_profile_data
) -> None:
    # Write after the app/client fixtures have started with a valid config.
    field, value = invalid_profile_data
    path = write_config(home, {"embedding": E5, "indexing": {field: value}})
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    response = await client.get("/api/config/defaults")
    assert response.status_code == 200, response.text
    assert response.json()["indexing"]["max_chunk_tokens"] == 384
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


@pytest.mark.parametrize("field,value", [("target_chunk_tokens", 300), ("max_chunk_tokens", 512)])
def test_set_can_repair_a_late_profile_budget_and_then_migrate(
    home: Path, monkeypatch: pytest.MonkeyPatch, field: str, value: int
) -> None:
    path = write_config(home, {"embedding": E5, "indexing": {"target_chunk_tokens": 400}})
    provider = home / "provider-memories"
    provider.mkdir()
    monkeypatch.setattr("memtomem.config._canonical_provider_dirs", lambda: [provider])
    with pytest.raises(ValueError, match="target_chunk_tokens"):
        build_fresh_config(migrate=False)
    result = CliRunner().invoke(cli, ["config", "set", f"indexing.{field}", str(value)])
    assert result.exit_code == 0, result.output
    fresh = build_fresh_config(migrate=False)
    assert getattr(fresh.indexing, field) == value
    assert json.loads(path.read_text())["indexing"][field] == value
    assert not fresh.indexing.auto_discover
    assert provider.resolve() in [
        entry.expanduser().resolve() for entry in fresh.indexing.memory_dirs
    ]


def test_unrelated_set_cannot_leave_a_repairable_profile_invalid(home: Path) -> None:
    path = write_config(home, {"embedding": E5, "indexing": {"target_chunk_tokens": 400}})
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    result = CliRunner().invoke(cli, ["config", "set", "mmr.enabled", "true"])
    assert result.exit_code == 1
    assert "target_chunk_tokens (400) must be <= max_chunk_tokens (384)" in result.output
    assert "validation error" not in result.output
    assert "https://errors.pydantic.dev" not in result.output
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
