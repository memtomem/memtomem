"""MCP persistence failures must restore the pre-edit runtime (#2436)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from memtomem import config as cfg_mod
from memtomem.config import Mem2MemConfig
from memtomem.config_signature import build_fresh_config
from memtomem.server.tools import status_config

from .helpers import isolate_config_paths


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    return isolate_config_paths(monkeypatch, tmp_path / "config-home") / "config.json"


def install_app(monkeypatch, config):
    app = SimpleNamespace(
        config=config,
        search_pipeline=SimpleNamespace(invalidate_cache=MagicMock(), config=config.search),
        storage=SimpleNamespace(rebuild_fts=AsyncMock(return_value=3)),
        embedder=SimpleNamespace(config=config.embedding),
    )
    monkeypatch.setattr(status_config, "_get_app_initialized", AsyncMock(return_value=app))
    return app


@pytest.mark.parametrize("failure", [ValueError, TimeoutError, PermissionError, RuntimeError])
@pytest.mark.parametrize("disk_state", ["changed", "invalid_json", "invalid_e5"])
@pytest.mark.parametrize("explicit", [False, True], ids=["default-field", "runtime-edit"])
async def test_failed_save_restores_pre_edit_runtime(
    failure, disk_state, explicit, config_path, monkeypatch
):
    embedding = {"provider": "onnx", "model": "intfloat/multilingual-e5-small"}
    config_path.write_text(
        json.dumps({"embedding": embedding, "indexing": {"auto_discover": False}}),
        encoding="utf-8",
    )
    config = build_fresh_config(migrate=False)
    app = install_app(monkeypatch, config)
    if explicit:
        assert (await status_config.mem_config("search.default_top_k", "7")).startswith("Set ")
    app.search_pipeline.invalidate_cache.reset_mock()
    before = config.model_dump()
    sections = {name: getattr(config, name) for name in ("search", "embedding", "indexing")}
    fields_set = {name: section.model_fields_set.copy() for name, section in sections.items()}
    config_fields_set = config.model_fields_set.copy()
    weights = config.search.rrf_weights

    if disk_state == "changed":
        disk = json.dumps({"embedding": embedding, "search": {"default_top_k": 31}})
    elif disk_state == "invalid_json":
        disk = "{invalid json"
    else:
        disk = json.dumps({"embedding": embedding, "indexing": {"chunk_model_tokens": 1024}})
    config_path.write_text(disk, encoding="utf-8")
    disk_before = config_path.read_bytes()
    if disk_state == "invalid_e5":
        with pytest.raises(ValueError, match="E5 requires exact chunk budgets"):
            build_fresh_config(migrate=False)

    save = MagicMock(side_effect=failure("injected save failure"))
    monkeypatch.setattr(cfg_mod, "save_config_overrides", save)
    # The mocked save does no I/O. Any loader call now belongs to rollback.
    loaders = []
    for name in ("load_config_d", "load_config_overrides"):
        loader = MagicMock(side_effect=AssertionError("rollback read disk"))
        monkeypatch.setattr(cfg_mod, name, loader)
        loaders.append(loader)
    fresh = MagicMock(side_effect=AssertionError("rollback rebuilt config"))
    monkeypatch.setattr("memtomem.config_signature.build_fresh_config", fresh)

    response = await status_config.mem_config("search.default_top_k", "17", persist=True)

    if failure is TimeoutError:
        assert response == (
            "Could not persist config: another process is writing "
            "config.json. Runtime change rolled back; retry in a moment."
        )
    elif failure is ValueError:
        assert (
            response
            == "Failed to persist config: injected save failure. Runtime change rolled back."
        )
    elif failure is PermissionError:
        assert response == "Error: injected save failure"
    else:
        assert response == "Error: internal error (RuntimeError: injected save failure)"
    assert app.config is config
    assert config.model_dump() == before
    assert config.model_fields_set == config_fields_set
    for name, section in sections.items():
        assert getattr(config, name) is section
        assert section.model_fields_set == fields_set[name]
    assert app.search_pipeline.config is config.search
    assert config.search.rrf_weights is weights
    assert app.embedder.config is config.embedding
    assert config.indexing.chunk_model_tokens == 512
    assert config.indexing.max_chunk_tokens == 384
    assert config_path.read_bytes() == disk_before
    save.assert_called_once_with(config)
    for loader in [*loaders, fresh]:
        loader.assert_not_called()
    app.search_pipeline.invalidate_cache.assert_not_called()
    app.storage.rebuild_fts.assert_not_called()


@pytest.mark.parametrize("failure", [ValueError, TimeoutError, PermissionError, RuntimeError])
@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("search.rrf_weights", "1.5,0.8"),
        ("search.tokenizer", "kiwipiepy"),
        ("embedding.onnx_batch_size", "64"),
    ],
)
async def test_failed_save_preserves_list_identity_and_skips_fanout(
    failure, key, value, config_path, monkeypatch
):
    config = Mem2MemConfig()
    app = install_app(monkeypatch, config)
    before = config.model_dump()
    search_fields = config.search.model_fields_set.copy()
    embedding_fields = config.embedding.model_fields_set.copy()
    weights = config.search.rrf_weights
    publish = MagicMock()
    tokenizer = MagicMock()
    monkeypatch.setattr(status_config, "publish_onnx_batch_size", publish)
    monkeypatch.setattr("memtomem.storage.fts_tokenizer.set_tokenizer", tokenizer)
    save = MagicMock(side_effect=failure("injected save failure"))
    monkeypatch.setattr(cfg_mod, "save_config_overrides", save)

    await status_config.mem_config(key, value, persist=True)

    save.assert_called_once_with(config)
    assert app.config is config
    assert config.model_dump() == before
    assert config.search.rrf_weights is weights
    assert config.search.model_fields_set == search_fields
    assert config.embedding.model_fields_set == embedding_fields
    assert app.search_pipeline.config is config.search
    assert app.embedder.config is config.embedding
    publish.assert_not_called()
    tokenizer.assert_not_called()
    app.search_pipeline.invalidate_cache.assert_not_called()
    app.storage.rebuild_fts.assert_not_called()


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("nodot", "1", "Key must be"),
        ("search.default_top_k.extra", "1", "Key must be"),
        ("unknown.field", "1", "Section 'unknown' not found"),
        ("search.unknown", "1", "Field 'unknown' not found"),
        ("embedding.provider", "none", "not mutable"),
        ("model_dump.field", "1", "not found"),
        ("search.model_dump", "1", "not mutable"),
        ("search.default_top_k", "invalid", "Invalid value"),
        ("search.rrf_weights", "1,2,3", "Invalid value"),
    ],
)
async def test_rejected_edit_does_not_save_or_fan_out(key, value, message, monkeypatch):
    config = Mem2MemConfig()
    app = install_app(monkeypatch, config)
    before = config.model_dump()
    save = MagicMock()
    monkeypatch.setattr(cfg_mod, "save_config_overrides", save)

    response = await status_config.mem_config(key, value, persist=True)

    assert message in response
    assert app.config is config
    assert config.model_dump() == before
    save.assert_not_called()
    app.search_pipeline.invalidate_cache.assert_not_called()


@pytest.mark.parametrize("persist", [False, True])
@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("search.default_top_k", "17"),
        ("search.tokenizer", "kiwipiepy"),
        ("embedding.onnx_batch_size", "64"),
    ],
)
async def test_success_preserves_persistence_and_fanout(
    key, value, persist, config_path, monkeypatch
):
    config = Mem2MemConfig()
    app = install_app(monkeypatch, config)
    publish = MagicMock()
    tokenizer = MagicMock()
    monkeypatch.setattr(status_config, "publish_onnx_batch_size", publish)
    monkeypatch.setattr("memtomem.storage.fts_tokenizer.set_tokenizer", tokenizer)
    save = MagicMock(wraps=cfg_mod.save_config_overrides)
    monkeypatch.setattr(cfg_mod, "save_config_overrides", save)

    response = await status_config.mem_config(key, value, persist=persist)

    assert response.startswith(f"Set {key}")
    section_name, field_name = key.split(".")
    actual = getattr(getattr(config, section_name), field_name)
    assert str(actual) == value
    assert app.config is config
    assert app.search_pipeline.config is config.search
    assert app.embedder.config is config.embedding
    if persist:
        save.assert_called_once_with(config)
        assert "(persisted to config.json)" in response
        assert (
            json.loads(config_path.read_text(encoding="utf-8"))[section_name][field_name] == actual
        )
    else:
        save.assert_not_called()
        assert "(runtime only — not persisted)" in response
        assert not config_path.exists()
    app.search_pipeline.invalidate_cache.assert_called_once_with()
    if key == "search.tokenizer":
        tokenizer.assert_called_once_with(actual)
        app.storage.rebuild_fts.assert_awaited_once_with()
        assert "FTS index rebuilt (3 chunks)." in response
    else:
        tokenizer.assert_not_called()
        app.storage.rebuild_fts.assert_not_called()
    if key == "embedding.onnx_batch_size":
        publish.assert_called_once_with(app.embedder, actual)
    else:
        publish.assert_not_called()
