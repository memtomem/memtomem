"""Web surfaces for a rejected config section (#2385 item 3).

Three behaviours, all against a real tmp HOME and the real load path:

* a stale ``embedding.dimension`` now fails a *re-read* loudly — the runtime
  keeps the previous config, the banner explains, and writes are refused;
* the running config's own rejected layers are reported on ``GET /api/config``
  as ``config_load_warnings``, which the reload error does not cover;
* a save that fails while disk is invalid still answers its own HTTP error
  instead of a 500, and leaves a banner behind.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from memtomem.web import hot_reload as _hot_reload
from memtomem.web.app import create_app

from .helpers import set_home

STALE_E5: dict[str, Any] = {
    "embedding": {
        "provider": "onnx",
        "model": "intfloat/multilingual-e5-small",
        "dimension": 1024,
    }
}


def _bump_mtime(path: Path) -> None:
    st = path.stat()
    new_ns = st.st_mtime_ns + 1_000_000
    os.utime(path, ns=(new_ns, new_ns))


def _write_config(home: Path, data: dict[str, Any]) -> Path:
    cfg = home / ".memtomem" / "config.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps(data), encoding="utf-8")
    _bump_mtime(cfg)
    return cfg


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    set_home(monkeypatch, tmp_path)
    for k in list(os.environ):
        if k.startswith("MEMTOMEM_"):
            monkeypatch.delenv(k, raising=False)
    return tmp_path


@pytest.fixture
def app(home: Path):
    application = create_app(lifespan=None, mode="dev")
    application.state.csrf_enforce = False

    storage = AsyncMock()
    storage.rebuild_fts = AsyncMock(return_value=0)
    search_pipeline = AsyncMock()
    search_pipeline.invalidate_cache = MagicMock()

    application.state.storage = storage
    application.state.search_pipeline = search_pipeline
    application.state.index_engine = AsyncMock()
    application.state.embedder = AsyncMock()
    application.state.dedup_scanner = AsyncMock()

    application.state.config = _hot_reload._build_fresh_config()
    application.state.config_signature = _hot_reload.current_signature()
    application.state.last_reload_error = None
    return application


@pytest.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


class TestRejectedSectionOnReRead:
    async def test_stale_dimension_keeps_the_running_config_and_explains(
        self, home: Path, app, client: AsyncClient
    ) -> None:
        _write_config(home, {"mmr": {"enabled": False}})
        app.state.config = _hot_reload._build_fresh_config()
        app.state.config_signature = _hot_reload.current_signature()

        _write_config(home, STALE_E5)

        resp = await client.get("/api/config")

        assert resp.status_code == 200
        data = resp.json()
        # Not just "some error": the message has to name what to fix, or a
        # green assertion here would also pass on an unrelated refusal (the
        # chunk-budget guard refuses a changed budget on its own).
        error = data["config_reload_error"]
        assert "ConfigError" in error
        assert "[embedding]" in error
        assert "dimension=384" in error
        # The previous config is still what is running.
        assert data["mmr"]["enabled"] is False

    async def test_writes_are_refused_while_the_section_is_rejected(
        self, home: Path, app, client: AsyncClient
    ) -> None:
        _write_config(home, {"mmr": {"enabled": False}})
        app.state.config = _hot_reload._build_fresh_config()
        app.state.config_signature = _hot_reload.current_signature()
        _write_config(home, STALE_E5)
        await client.get("/api/config")

        resp = await client.patch("/api/config", json={"search": {"default_top_k": 5}})

        assert resp.status_code == 409, resp.text

    async def test_a_corrected_file_clears_the_banner(
        self, home: Path, app, client: AsyncClient
    ) -> None:
        _write_config(home, {"mmr": {"enabled": False}})
        app.state.config = _hot_reload._build_fresh_config()
        app.state.config_signature = _hot_reload.current_signature()
        _write_config(home, STALE_E5)
        assert (await client.get("/api/config")).json()["config_reload_error"] is not None

        _write_config(home, {"mmr": {"enabled": True}})

        data = (await client.get("/api/config")).json()
        assert data["config_reload_error"] is None
        assert data["mmr"]["enabled"] is True


class TestLoadWarningsOnTheRunningConfig:
    async def test_get_config_reports_the_rejected_layer(
        self, home: Path, app, client: AsyncClient
    ) -> None:
        """Startup is tolerant, so the server *runs* on a config missing the
        section. ``config_reload_error`` says nothing about that — it only
        covers a failed re-read."""
        from memtomem.config_signature import build_fresh_config

        _write_config(home, STALE_E5)
        app.state.config = build_fresh_config(migrate=False, strict_overrides=False)
        app.state.config_signature = _hot_reload.current_signature()

        data = (await client.get("/api/config")).json()

        assert data["config_reload_error"] is None
        (warning,) = data["config_load_warnings"]
        assert warning["section"] == "embedding"
        assert "dimension=384" in warning["error"]
        assert warning["path"].endswith("config.json")
        assert data["embedding"]["provider"] == "none"

    async def test_a_clean_config_reports_no_warnings(
        self, home: Path, app, client: AsyncClient
    ) -> None:
        _write_config(home, {"mmr": {"enabled": True}})
        app.state.config = _hot_reload._build_fresh_config()
        app.state.config_signature = _hot_reload.current_signature()

        data = (await client.get("/api/config")).json()

        assert data["config_load_warnings"] == []

    async def test_a_save_that_repairs_the_file_clears_the_warning(
        self, home: Path, app, client: AsyncClient
    ) -> None:
        """A successful write can fix the very section a load rejected.

        The write is delta-only, so a section matching the lower layers is
        dropped from the file entirely. The writer then banks the new
        signature, which is exactly what stops a reload from replacing the
        record — so the warning outlived the file that caused it and kept
        telling the user to fix something already valid.
        """
        from memtomem.config_signature import build_fresh_config

        _write_config(home, {"rerank": {"enabled": False, "min_pool": 100, "max_pool": 10}})
        app.state.config = build_fresh_config(migrate=False, strict_overrides=False)
        app.state.config_signature = _hot_reload.current_signature()
        assert (await client.get("/api/config")).json()["config_load_warnings"]

        resp = await client.patch("/api/config?persist=true", json={"search": {"default_top_k": 7}})
        assert resp.status_code == 200, resp.text
        # The rejected section is gone from disk, so a fresh reader sees none.
        assert build_fresh_config(migrate=False, strict_overrides=False).load_diagnostics == ()

        assert (await client.get("/api/config")).json()["config_load_warnings"] == []
        # And it stays cleared on the next read, not just the first.
        assert (await client.get("/api/config")).json()["config_load_warnings"] == []

    async def test_a_save_does_not_clear_a_rejection_it_did_not_repair(
        self, home: Path, app, client: AsyncClient
    ) -> None:
        """The counterpart: a save never touches ``config.d``, so a fragment's
        rejection must survive one. Blindly clearing the list would hide it.
        """
        from memtomem.config_signature import build_fresh_config

        fragment = home / ".memtomem" / "config.d" / "10-embedding.json"
        fragment.parent.mkdir(parents=True, exist_ok=True)
        fragment.write_text(json.dumps(STALE_E5), encoding="utf-8")
        _write_config(home, {"mmr": {"enabled": False}})
        app.state.config = build_fresh_config(migrate=False, strict_overrides=False)
        app.state.config_signature = _hot_reload.current_signature()

        resp = await client.patch("/api/config?persist=true", json={"search": {"default_top_k": 7}})
        assert resp.status_code == 200, resp.text

        (warning,) = (await client.get("/api/config")).json()["config_load_warnings"]
        assert warning["section"] == "embedding"
        assert warning["path"].endswith("10-embedding.json")

    def test_the_field_defaults_to_empty_when_a_response_omits_it(self) -> None:
        """The route always passes the field, so its schema default is not
        exercised by the request tests — but any other producer of a
        ``ConfigResponse`` relies on it being a list, not ``None``."""
        from memtomem.web.schemas.config import ConfigResponse

        first = ConfigResponse.model_construct()
        second = ConfigResponse.model_construct()

        assert first.config_load_warnings == []
        # The field carries a mutable default, so the two instances must not
        # be handed the same list — appending to one would grow the other.
        first.config_load_warnings.append("x")
        assert second.config_load_warnings == []


class TestRollbackWhenDiskIsInvalid:
    """A failed save must still answer its own error.

    The handlers mutate ``app.state.config`` before persisting and revert by
    re-reading the file. That read can itself fail — which used to replace the
    handler's 400/503 with a 500 and skip its cleanup.
    """

    async def test_save_failure_over_invalid_disk_answers_503_not_500(
        self, home: Path, app, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_config(home, {"mmr": {"enabled": False}})
        app.state.config = _hot_reload._build_fresh_config()
        app.state.config_signature = _hot_reload.current_signature()

        def _boom(_cfg):
            raise TimeoutError("lock held by another process")

        monkeypatch.setattr("memtomem.web.routes.system.save_config_overrides", _boom)
        # Disk is invalid at the moment the handler tries to revert, and the
        # signature is pinned so the pre-save reload does not refuse first.
        cfg_path = home / ".memtomem" / "config.json"
        cfg_path.write_text(json.dumps(STALE_E5), encoding="utf-8")
        app.state.config_signature = _hot_reload.current_signature()

        resp = await client.patch("/api/config?persist=true", json={"search": {"default_top_k": 5}})

        assert resp.status_code == 503, resp.text

    async def test_a_failed_revert_leaves_a_banner_and_closes_the_gate(
        self, home: Path, app, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_config(home, {"mmr": {"enabled": False}})
        app.state.config = _hot_reload._build_fresh_config()
        app.state.config_signature = _hot_reload.current_signature()

        def _boom(_cfg):
            raise TimeoutError("lock held by another process")

        monkeypatch.setattr("memtomem.web.routes.system.save_config_overrides", _boom)
        cfg_path = home / ".memtomem" / "config.json"
        cfg_path.write_text(json.dumps(STALE_E5), encoding="utf-8")
        app.state.config_signature = _hot_reload.current_signature()
        await client.patch("/api/config?persist=true", json={"search": {"default_top_k": 5}})

        err = _hot_reload.get_reload_error(app)
        assert err is not None and "[embedding]" in err.message
        # And the gate is shut for the next writer.
        again = await client.patch(
            "/api/config?persist=true", json={"search": {"default_top_k": 6}}
        )
        assert again.status_code == 409, again.text

    async def test_a_revision_saved_during_the_failing_revert_is_not_marked_seen(
        self, home: Path, app, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The error must be bound to the revision that was *read*.

        Binding it to whatever is on disk after the failure marks a good
        revision as already-seen, so ``reload_error_is_stale`` never releases
        the banner and writes stay refused until the next edit.
        """
        _write_config(home, {"mmr": {"enabled": False}})
        app.state.config = _hot_reload._build_fresh_config()
        app.state.config_signature = _hot_reload.current_signature()

        cfg_path = home / ".memtomem" / "config.json"
        cfg_path.write_text(json.dumps(STALE_E5), encoding="utf-8")
        _bump_mtime(cfg_path)

        def _rebuild_then_someone_fixes_it():
            # Another process corrects the file while this read is in flight.
            cfg_path.write_text(json.dumps({"mmr": {"enabled": True}}), encoding="utf-8")
            # Writes can share a clock tick on Windows. Advance from the
            # sampled revision, not from the timestamp reset by this write.
            newer_mtime_ns = read_mtime_ns + 1_000_000_000
            os.utime(cfg_path, ns=(newer_mtime_ns, newer_mtime_ns))
            raise ValueError("Invalid config section [embedding] in config.json")

        # What the read is about to see. Captured here because the rebuild
        # below moves both axes before it fails.
        read_mtime_ns = _hot_reload.get_config_mtime_ns()
        read_signature = _hot_reload.current_signature()

        monkeypatch.setattr(_hot_reload, "_build_fresh_config", _rebuild_then_someone_fixes_it)
        _hot_reload.revert_runtime_to_disk(app)

        err = _hot_reload.get_reload_error(app)
        assert err is not None
        # Each axis separately: ``reload_error_is_stale`` is an OR, so
        # asserting only through it lets one axis regress unnoticed while the
        # other still differs.
        assert err.at_mtime_ns == read_mtime_ns, "the error was bound to a later mtime"
        assert err.at_signature == read_signature, "the error was bound to a later signature"
        assert err.at_mtime_ns != _hot_reload.get_config_mtime_ns()
        assert _hot_reload.reload_error_is_stale(err), (
            "the failure was bound to the corrected revision, so it can never be released"
        )

    async def test_a_revision_saved_during_a_successful_revert_is_still_reloaded(
        self, home: Path, app, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The same pre-rebuild sampling on the path that *succeeds*.

        The signature recorded after a revert has to describe the revision the
        rebuild actually read. Recording what is on disk afterwards — which is
        what the inline code this helper replaced did — marks an edit that
        landed during the read as already seen, and it is never loaded.
        """
        _write_config(home, {"mmr": {"enabled": False}})
        app.state.config = _hot_reload._build_fresh_config()
        app.state.config_signature = _hot_reload.current_signature()

        cfg_path = home / ".memtomem" / "config.json"
        real_build = _hot_reload._build_fresh_config

        def _rebuild_while_someone_saves():
            built = real_build()
            # Another process saves a newer revision during this read.
            cfg_path.write_text(json.dumps({"mmr": {"enabled": True}}), encoding="utf-8")
            _bump_mtime(cfg_path)
            return built

        monkeypatch.setattr(_hot_reload, "_build_fresh_config", _rebuild_while_someone_saves)
        _hot_reload.revert_runtime_to_disk(app)
        monkeypatch.setattr(_hot_reload, "_build_fresh_config", real_build)

        # The revert succeeded, so no banner — but the newer revision must
        # still look unseen to the next reader.
        assert _hot_reload.get_reload_error(app) is None
        assert app.state.config.mmr.enabled is False

        data = (await client.get("/api/config")).json()

        assert data["mmr"]["enabled"] is True, (
            "the revision saved during the revert was marked seen and never loaded"
        )
