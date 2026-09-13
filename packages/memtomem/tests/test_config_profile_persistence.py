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
        assert "a lower layer (default, embedding profile, or config.d)" in result.output


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


def test_unnormalized_config_save_does_not_pin_profile_budgets(home: Path) -> None:
    """A config assembled without profile normalization still holds non-E5
    budgets in its unset fields; saving it must not write them (#2399 review)."""
    from memtomem.config import Mem2MemConfig, load_config_d, load_config_overrides

    path = write_config(home, {"embedding": E5, "indexing": {"auto_discover": False}})
    cfg = Mem2MemConfig()
    load_config_d(cfg, quiet=True)
    load_config_overrides(cfg, migrate=False)
    assert cfg.indexing.max_chunk_tokens == 512
    before = (cfg.indexing.model_dump(), cfg.indexing.model_fields_set.copy())
    cfg.search.default_top_k = 17
    save_config_overrides(cfg)
    assert json.loads(path.read_text())["indexing"] == {"auto_discover": False}
    assert (cfg.indexing.model_dump(), cfg.indexing.model_fields_set) == before
    fresh = build_fresh_config(migrate=False)
    assert (fresh.indexing.max_chunk_tokens, fresh.search.default_top_k) == (384, 17)


async def test_mcp_persist_rollback_keeps_profile_budgets(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed ``mem_config`` persist restores the runtime through the canonical
    load, so the next persist does not pin non-E5 budgets (#2399 review)."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from memtomem import config as config_mod
    from memtomem.server.tools import status_config

    path = write_config(home, {"embedding": E5, "indexing": {"auto_discover": False}})
    app = SimpleNamespace(
        config=build_fresh_config(migrate=False),
        search_pipeline=MagicMock(),
        embedder=MagicMock(),
        storage=AsyncMock(),
    )
    monkeypatch.setattr(status_config, "_get_app_initialized", AsyncMock(return_value=app))
    real_save = config_mod.save_config_overrides

    def locked(_config):
        raise TimeoutError

    monkeypatch.setattr(config_mod, "save_config_overrides", locked)
    out = await status_config.mem_config(key="search.default_top_k", value="17", persist=True)
    assert "rolled back" in out
    assert app.config.indexing.max_chunk_tokens == 384
    assert app.config.search.default_top_k != 17

    monkeypatch.setattr(config_mod, "save_config_overrides", real_save)
    out = await status_config.mem_config(key="mmr.enabled", value="true", persist=True)
    assert "persisted to config.json" in out, out
    assert json.loads(path.read_text())["indexing"] == {"auto_discover": False}
    assert build_fresh_config(migrate=False).indexing.max_chunk_tokens == 384


def _revert_runtime_identity(cfg, embedding: dict) -> None:
    """Switch the live identity the way ``_revert_to_stored_locked`` does:
    plain field assignment, no profile normalization, file untouched."""
    cfg.embedding.provider = embedding["provider"]
    cfg.embedding.model = embedding["model"]


# (file identity, runtime identity after the revert, file's resolved budget)
DIVERGED = [(BGE, E5, 512), (E5, BGE, 384)]


@pytest.mark.parametrize("file_model,runtime_model,file_budget", DIVERGED, ids=["to-e5", "to-bge"])
def test_diverged_runtime_identity_keeps_a_requested_budget(
    home: Path, file_model: dict, runtime_model: dict, file_budget: int
) -> None:
    """Saves judge pins on the identity the file reloads, not the runtime's
    (#2399 review). The requested value survives, and nothing else is pinned."""
    from memtomem.config import assign_section_fields

    path = write_config(home, {"embedding": file_model, "indexing": {"auto_discover": False}})
    cfg = build_fresh_config(migrate=False)
    _revert_runtime_identity(cfg, runtime_model)
    requested = 448
    assign_section_fields(cfg.indexing, {"max_chunk_tokens": requested})
    save_config_overrides(cfg)
    data = json.loads(path.read_text())
    assert data == {
        "embedding": file_model,
        "indexing": {"auto_discover": False, "max_chunk_tokens": requested},
    }
    assert build_fresh_config(migrate=False).indexing.max_chunk_tokens == requested
    # Asking for the file's own budget is a no-op pin, whatever the runtime holds.
    assign_section_fields(cfg.indexing, {"max_chunk_tokens": file_budget})
    save_config_overrides(cfg)
    assert json.loads(path.read_text())["indexing"] == {"auto_discover": False}
    assert build_fresh_config(migrate=False).indexing.max_chunk_tokens == file_budget


@pytest.mark.parametrize("file_model,runtime_model,file_budget", DIVERGED, ids=["to-e5", "to-bge"])
def test_diverged_runtime_identity_unrelated_save_pins_nothing(
    home: Path, file_model: dict, runtime_model: dict, file_budget: int
) -> None:
    path = write_config(home, {"embedding": file_model, "indexing": {"auto_discover": False}})
    cfg = build_fresh_config(migrate=False)
    _revert_runtime_identity(cfg, runtime_model)
    cfg.mmr.enabled = True
    save_config_overrides(cfg)
    assert json.loads(path.read_text()) == {
        "embedding": file_model,
        "indexing": {"auto_discover": False},
        "mmr": {"enabled": True},
    }
    fresh = build_fresh_config(migrate=False)
    assert (fresh.indexing.max_chunk_tokens, fresh.mmr.enabled) == (file_budget, True)


@pytest.mark.parametrize("file_model,runtime_model,file_budget", DIVERGED, ids=["to-e5", "to-bge"])
async def test_web_reset_value_is_what_save_prunes_on_a_diverged_runtime(
    home: Path, app, client, file_model: dict, runtime_model: dict, file_budget: int
) -> None:
    """↺ and Save share one baseline, so a stale runtime cannot split them."""
    from memtomem.config import assign_section_fields

    path = write_config(home, {"embedding": file_model, "indexing": {"max_chunk_tokens": 448}})
    app.state.config = build_fresh_config(migrate=False)
    hot_reload.initialize_reload_state(app)
    _revert_runtime_identity(app.state.config, runtime_model)
    response = await client.get("/api/config/defaults")
    assert response.status_code == 200, response.text
    reset_value = response.json()["indexing"]["max_chunk_tokens"]
    assert reset_value == file_budget
    assign_section_fields(app.state.config.indexing, {"max_chunk_tokens": reset_value})
    receipt = save_config_overrides(app.state.config)
    assert receipt.pruned("indexing", "max_chunk_tokens")
    assert "indexing" not in json.loads(path.read_text())


def _select_e5(home: Path, monkeypatch: pytest.MonkeyPatch, source: str, config: dict) -> Path:
    if source == "env":
        monkeypatch.setenv("MEMTOMEM_EMBEDDING", json.dumps(E5))
    elif source == "fragment":
        (home / ".memtomem" / "config.d" / "10-model.json").write_text(
            json.dumps({"embedding": E5}), encoding="utf-8"
        )
    elif source == "override-after-indexing":
        # The file lists indexing before the embedding that selects E5.
        config = {**config, "embedding": E5}
    else:
        config = {"embedding": E5, **config}
    return write_config(home, config)


SOURCES = ["env", "fragment", "override", "override-after-indexing"]


@pytest.mark.parametrize("source", SOURCES)
def test_loader_accepts_a_budget_valid_only_under_the_profile(
    home: Path, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    """``max_chunk_tokens=320`` is valid under E5 (generated target 320) and
    invalid under the generic target 384; the loader must judge it on E5."""
    _select_e5(
        home, monkeypatch, source, {"indexing": {"auto_discover": False, "max_chunk_tokens": 320}}
    )
    cfg = build_fresh_config(migrate=False)
    assert cfg.load_diagnostics == ()
    assert (cfg.indexing.max_chunk_tokens, cfg.indexing.target_chunk_tokens) == (320, 320)
    assert cfg.indexing.auto_discover is False
    assert "target_chunk_tokens" not in cfg.indexing.model_fields_set


@pytest.mark.parametrize("source", ["env", "fragment", "override"])
def test_fragment_budget_is_judged_on_the_selected_profile(
    home: Path, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    _select_e5(home, monkeypatch, source, {"indexing": {"auto_discover": False}})
    (home / ".memtomem" / "config.d" / "20-budget.json").write_text(
        json.dumps({"indexing": {"max_chunk_tokens": 320}}), encoding="utf-8"
    )
    cfg = build_fresh_config(migrate=False)
    if source == "override":
        # config.d loads before config.json selects E5, so the fragment is
        # judged on the generic profile — and the complete load reports it.
        # Known limitation, same as main (tracked separately).
        assert [d.section for d in cfg.load_diagnostics] == ["indexing"]
        return
    assert cfg.load_diagnostics == ()
    assert (cfg.indexing.max_chunk_tokens, cfg.indexing.target_chunk_tokens) == (320, 320)


def test_cli_set_below_the_generic_target_round_trips(home: Path) -> None:
    path = write_config(home, {"embedding": E5, "indexing": {"auto_discover": False}})
    result = CliRunner().invoke(cli, ["config", "set", "indexing.max_chunk_tokens", "320"])
    assert result.exit_code == 0, result.output
    assert json.loads(path.read_text())["indexing"] == {
        "auto_discover": False,
        "max_chunk_tokens": 320,
    }
    cfg = build_fresh_config(migrate=False)
    assert cfg.load_diagnostics == ()
    assert (cfg.indexing.max_chunk_tokens, cfg.indexing.target_chunk_tokens) == (320, 320)


def test_generic_profile_still_rejects_the_budget(home: Path) -> None:
    write_config(
        home, {"embedding": BGE, "indexing": {"auto_discover": False, "max_chunk_tokens": 320}}
    )
    cfg = build_fresh_config(migrate=False, strict_overrides=False)
    assert [d.section for d in cfg.load_diagnostics] == ["indexing"]
    assert cfg.indexing.max_chunk_tokens == 512


def test_budget_accepted_under_e5_fails_loudly_when_a_later_layer_leaves_e5(home: Path) -> None:
    """A fragment budget validated under E5 must not survive silently as an
    invalid combination once config.json switches the model away."""
    (home / ".memtomem" / "config.d" / "10-e5.json").write_text(
        json.dumps({"embedding": E5, "indexing": {"max_chunk_tokens": 320}}), encoding="utf-8"
    )
    write_config(home, {"embedding": BGE})
    with pytest.raises(ValueError, match="target_chunk_tokens"):
        build_fresh_config(migrate=False)
    view = build_fresh_config(migrate=False, strict_overrides=False, validate_profile=False)
    assert view.indexing.max_chunk_tokens == 320
