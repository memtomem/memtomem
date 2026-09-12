"""A rejected save must preserve the runtime that preceded it (#2409)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from memtomem.web import hot_reload

from .test_web_hot_reload import _write_config, app as app, client as client, home as home


@pytest.mark.parametrize("operation", ["patch", "save", "add", "remove"])
@pytest.mark.parametrize("failure", [TimeoutError, ValueError, PermissionError])
@pytest.mark.parametrize("invalid_disk", [False, True])
async def test_failed_save_preserves_runtime_and_shared_sections(
    operation, failure, invalid_disk, home, app, client, monkeypatch
):
    first, second, third = (home / name for name in ("first", "second", "third"))
    for directory in (first, second, third):
        directory.mkdir()
    path = _write_config(
        home,
        {"search": {"default_top_k": 5}, "indexing": {"memory_dirs": [str(first), str(second)]}},
    )
    app.state.config = hot_reload._build_fresh_config()
    app.state.config_signature = hot_reload.current_signature()
    # A prior successful runtime-only edit must survive this failed save,
    # even when the file is valid and differs from the running config.
    assert (
        await client.patch("/api/config", json={"search": {"default_top_k": 7}})
    ).status_code == 200
    config = app.state.config
    before = config.model_dump()
    search, indexing = config.search, config.indexing
    dirs = indexing.memory_dirs
    fields_set = search.model_fields_set.copy()
    signature = app.state.config_signature
    disk_before = path.read_bytes()
    app.state.index_engine._config = indexing
    watcher = AsyncMock()
    watcher._config = indexing
    app.state.file_watcher = watcher
    app.state.search_pipeline.invalidate_cache.reset_mock()

    def fail_save(_config):
        # Change disk AFTER the route's reload/gate, exercising a real race
        # rather than bypassing the pre-save safety check.
        if invalid_disk:
            path.write_text("{invalid json", encoding="utf-8")
        raise failure("injected save failure")

    monkeypatch.setattr("memtomem.web.routes.system.save_config_overrides", fail_save)
    if operation == "patch":
        call = client.patch(
            "/api/config?persist=true",
            json={"search": {"default_top_k": 20}, "mmr": {"enabled": True}},
        )
    elif operation == "save":
        call = client.post("/api/config/save")
    elif operation == "add":
        call = client.post("/api/memory-dirs/add", json={"path": str(third)})
    else:
        call = client.post(
            "/api/memory-dirs/remove", json={"path": str(second), "delete_chunks": True}
        )

    # Keep each route's existing HTTP mapping; unhandled storage errors must
    # still propagate after rollback, rather than being mislabeled as 400.
    if failure is PermissionError:
        with pytest.raises(failure, match="injected save failure"):
            await call
    else:
        response = await call
        assert response.status_code == (503 if failure is TimeoutError else 400), response.text

    assert app.state.config is config
    assert config.model_dump() == before
    assert config.search is search
    assert config.indexing is indexing
    assert indexing.memory_dirs is dirs
    assert search.model_fields_set == fields_set
    assert watcher._config is indexing
    assert app.state.index_engine._config is indexing
    assert app.state.config_signature == signature
    assert path.read_bytes() == (b"{invalid json" if invalid_disk else disk_before)
    watcher.reconfigure.assert_not_called()
    app.state.index_engine.index_path.assert_not_called()
    app.state.storage.get_source_files_with_counts.assert_not_called()
    app.state.storage.delete_by_source.assert_not_called()
    app.state.search_pipeline.invalidate_cache.assert_not_called()
    if invalid_disk:
        assert hot_reload.get_reload_error(app) is not None
        assert (await client.post("/api/config/save")).status_code == 409
        _write_config(home, {"search": {"default_top_k": 11}})
        response = await client.get("/api/config")
        assert response.json()["config_reload_error"] is None
        assert app.state.config.search.default_top_k == 11
    else:
        assert hot_reload.get_reload_error(app) is None
        assert (await client.get("/api/config")).json()["search"]["default_top_k"] == 7


@pytest.mark.parametrize("extra_section", [None, "model_dump", "model_config", "load_diagnostics"])
@pytest.mark.parametrize("failure", [TimeoutError, ValueError, PermissionError])
async def test_failed_multi_section_patch_discards_reranker_without_fanout(
    extra_section, failure, home, app, client, monkeypatch
):
    path = _write_config(home, {"search": {"tokenizer": "unicode61"}})
    app.state.config = hot_reload._build_fresh_config()
    app.state.config_signature = hot_reload.current_signature()
    config = app.state.config
    before = config.model_dump()
    old_rerank = config.rerank
    candidate = MagicMock()
    candidate.close = AsyncMock()
    monkeypatch.setattr("memtomem.web.routes.system.create_reranker", lambda _: candidate)
    monkeypatch.setattr("memtomem.web.routes.system._validate_reranker_ready", AsyncMock())
    set_tokenizer = MagicMock()
    monkeypatch.setattr("memtomem.storage.fts_tokenizer.set_tokenizer", set_tokenizer)
    publish_batch = MagicMock()
    monkeypatch.setattr("memtomem.web.routes.system.publish_onnx_batch_size", publish_batch)

    def fail_save(_config):
        path.write_text("{invalid json", encoding="utf-8")
        raise failure("injected save failure")

    monkeypatch.setattr("memtomem.web.routes.system.save_config_overrides", fail_save)
    payload = {
        "rerank": {"enabled": True},
        "search": {"tokenizer": "kiwipiepy", "default_top_k": 20},
        "embedding": {"onnx_batch_size": 64},
    }
    if extra_section is not None:
        payload[extra_section] = {}
    call = client.patch(
        "/api/config?persist=true",
        json=payload,
    )
    if failure is PermissionError:
        with pytest.raises(PermissionError, match="injected save failure"):
            await call
    else:
        response = await call
        assert response.status_code == (503 if failure is TimeoutError else 400), response.text
    assert app.state.config is config
    assert config.model_dump() == before
    assert config.rerank is old_rerank
    candidate.close.assert_awaited_once()
    app.state.search_pipeline.swap_reranker.assert_not_called()
    app.state.search_pipeline.invalidate_cache.assert_not_called()
    app.state.storage.rebuild_fts.assert_not_called()
    set_tokenizer.assert_not_called()
    publish_batch.assert_not_called()
    assert hot_reload.get_reload_error(app) is not None


@pytest.mark.parametrize("value", [1, True, "future", [], ["entry"], {}])
@pytest.mark.parametrize("with_valid_edit", [False, True])
async def test_unknown_section_is_rejected_before_snapshotting(
    value, with_valid_edit, home, app, client, monkeypatch
):
    save = MagicMock()
    monkeypatch.setattr("memtomem.web.routes.system.save_config_overrides", save)
    payload = {"future_section": value}
    if with_valid_edit:
        payload["search"] = {"default_top_k": 20}
    response = await client.patch("/api/config?persist=true", json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["rejected"] == ["future_section: unknown section"]
    if with_valid_edit:
        assert [change["field"] for change in response.json()["applied"]] == [
            "search.default_top_k"
        ]
        assert app.state.config.search.default_top_k == 20
        save.assert_called_once()
    else:
        assert response.json()["applied"] == []
        save.assert_not_called()


async def test_failed_patch_snapshot_is_taken_after_external_reload(home, app, client, monkeypatch):
    _write_config(home, {"search": {"default_top_k": 11}})
    signatures = []

    def fail_save(_config):
        signatures.append(app.state.config_signature)
        raise TimeoutError("locked")

    monkeypatch.setattr("memtomem.web.routes.system.save_config_overrides", fail_save)
    response = await client.patch(
        "/api/config?persist=true", json={"search": {"default_top_k": 20}}
    )
    assert response.status_code == 503
    assert app.state.config.search.default_top_k == 11
    assert app.state.config_signature == signatures[0]


async def test_failed_patch_does_not_replace_a_newer_runtime(home, app, client, monkeypatch):
    original = app.state.config
    before = original.model_dump()
    newer = original.model_copy(deep=True)
    newer.search.default_top_k = 11

    async def concurrent_reload(_reranker):
        # The PATCH awaits candidate readiness. A lock-free GET can publish
        # a fresh config during that await; rollback must not replace it.
        app.state.config = newer

    monkeypatch.setattr("memtomem.web.routes.system.create_reranker", lambda _: None)
    monkeypatch.setattr("memtomem.web.routes.system._validate_reranker_ready", concurrent_reload)

    def fail_save(_config):
        raise TimeoutError("locked")

    monkeypatch.setattr("memtomem.web.routes.system.save_config_overrides", fail_save)
    response = await client.patch(
        "/api/config?persist=true",
        json={"rerank": {"enabled": True}, "search": {"default_top_k": 20}},
    )
    assert response.status_code == 503
    assert app.state.config is newer
    assert newer.search.default_top_k == 11
    assert original.model_dump() == before
