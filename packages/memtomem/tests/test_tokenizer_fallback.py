"""A ``kiwipiepy`` store tokenized as ``unicode61`` says so in ``mem_status`` (#2647).

Every plugin launcher installs ``memtomem[onnx]``, so a server sharing a store
configured with the ``korean`` preset has no ``kiwipiepy``. Its backend then
tokenizes as ``unicode61`` from the first kiwipiepy call on and keeps writing: FTS
content is pre-tokenized in Python, so one index ends up holding two token
representations and Korean keyword queries miss rows across them.
``TestMixedStoreRecall`` pins the damage and the recovery the warning's ``fix``
names; ``TestFallbackWarning`` pins the warning itself.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from click.testing import CliRunner
from helpers import make_chunk, set_home

from memtomem import __version__
from memtomem.cli import cli
from memtomem.config import Mem2MemConfig, StorageConfig
from memtomem.server.tools import status_config
from memtomem.server.tools.status_config import _tokenizer_fallback_warning
from memtomem.storage import fts_tokenizer
from memtomem.storage.sqlite_backend import SqliteBackend

_ROW_A = "마케팅 예산을 삭감하기로 결정했다"
_ROW_B = "영업 예산을 동결하기로 결정했다"


@pytest.fixture
def restore_tokenizer(monkeypatch):
    """Whether kiwipiepy failed to import is process-wide; start clean, hand it back."""
    monkeypatch.setattr(fts_tokenizer, "_kiwi_instance", None)
    monkeypatch.setattr(fts_tokenizer, "_kiwi_unavailable", False)
    yield
    monkeypatch.delitem(sys.modules, "kiwipiepy", raising=False)


@pytest.fixture
def backend(tmp_path):
    """A kiwipiepy backend for the tests that only read its tokenizer state."""
    return SqliteBackend(StorageConfig(sqlite_path=tmp_path / "w.db"), fts_tokenizer="kiwipiepy")


def _fall_back(monkeypatch, storage) -> None:
    """A process without the extra: block the import, and trip the fallback.

    Clearing the loaded Kiwi first matters, since a process that already
    holds one keeps using it.
    """
    monkeypatch.setitem(sys.modules, "kiwipiepy", None)
    monkeypatch.setattr(fts_tokenizer, "_kiwi_instance", None)
    storage.set_fts_tokenizer("kiwipiepy")
    fts_tokenizer.tokenize_for_fts("워밍업", tokenizer=storage.fts_tokenizer)
    assert storage.fts_tokenizer_effective == "unicode61", "the fallback did not trip"


def _use_kiwi(monkeypatch, storage) -> None:
    """A restarted process with the extra installed: the sticky fallback is gone."""
    monkeypatch.delitem(sys.modules, "kiwipiepy", raising=False)
    monkeypatch.setattr(fts_tokenizer, "_kiwi_instance", None)
    monkeypatch.setattr(fts_tokenizer, "_kiwi_unavailable", False)
    storage.set_fts_tokenizer("kiwipiepy")


class TestMixedStoreRecall:
    @pytest.fixture(autouse=True)
    def _kiwi(self, restore_tokenizer):
        pytest.importorskip("kiwipiepy", reason="the korean extra is not installed")

    async def _mixed_store(self, storage, monkeypatch) -> dict[str, str]:
        _use_kiwi(monkeypatch, storage)
        a = make_chunk(_ROW_A, source="a.md")
        await storage.upsert_chunks([a])
        _fall_back(monkeypatch, storage)
        b = make_chunk(_ROW_B, source="b.md")
        await storage.upsert_chunks([b])
        return {a.id: "a", b.id: "b"}

    async def _found(self, storage, ids, query) -> list[str]:
        return sorted(ids[r.chunk.id] for r in await storage.bm25_search(query, top_k=10))

    @pytest.mark.asyncio
    async def test_an_inflected_query_misses_rows_written_the_other_way(self, storage, monkeypatch):
        ids = await self._mixed_store(storage, monkeypatch)

        _use_kiwi(monkeypatch, storage)
        assert await self._found(storage, ids, "예산을") == ["a"]
        _fall_back(monkeypatch, storage)
        assert await self._found(storage, ids, "예산을") == ["b"]
        # A bare stem still reaches both through unicode61's prefix ``*``,
        # which is why casual testing does not show the split.
        assert await self._found(storage, ids, "예산") == ["a", "b"]

    @pytest.mark.asyncio
    async def test_a_rebuild_under_kiwipiepy_restores_both_rows(self, storage, monkeypatch):
        ids = await self._mixed_store(storage, monkeypatch)

        _use_kiwi(monkeypatch, storage)
        await storage.rebuild_fts()

        assert await self._found(storage, ids, "예산을") == ["a", "b"]
        assert await self._found(storage, ids, "결정했다") == ["a", "b"]


class TestFallbackWarning:
    @pytest.fixture(autouse=True)
    def _restore(self, restore_tokenizer):
        pass

    @staticmethod
    def _installed(monkeypatch, available: bool) -> None:
        monkeypatch.setattr(
            status_config,
            "_dependency_state",
            lambda module, distribution=None: {"available": available, "version": None},
        )

    @staticmethod
    def _warning(backend):
        return _tokenizer_fallback_warning(backend.fts_tokenizer, backend)

    def test_a_unicode61_store_has_nothing_to_warn_about(self, monkeypatch, backend):
        self._installed(monkeypatch, False)
        backend.set_fts_tokenizer("unicode61")

        assert self._warning(backend) is None

    def test_a_working_kiwipiepy_store_has_nothing_to_warn_about(self, monkeypatch, backend):
        self._installed(monkeypatch, True)

        assert self._warning(backend) is None

    def test_a_tripped_fallback_warns_on_every_call(self, monkeypatch, backend):
        _fall_back(monkeypatch, backend)
        self._installed(monkeypatch, False)

        first = self._warning(backend)
        second = self._warning(backend)

        assert first is not None and first == second
        assert first["kind"] == "tokenizer_fallback"
        assert (first["configured"], first["active"]) == ("kiwipiepy", "unicode61")

    def test_installing_the_module_later_does_not_clear_it(self, monkeypatch, backend):
        """The fallback is sticky per process: only a restart undoes it."""
        _fall_back(monkeypatch, backend)
        self._installed(monkeypatch, True)
        monkeypatch.delitem(sys.modules, "kiwipiepy")  # importable again

        fts_tokenizer.tokenize_for_fts("워밍업", tokenizer=backend.fts_tokenizer)
        warning = self._warning(backend)

        assert warning is not None
        assert warning["active"] == "unicode61"

    def test_a_pending_fallback_warns_before_the_first_tokenize(self, monkeypatch, backend):
        """Status never tokenizes, so a missing module has not tripped it yet."""
        self._installed(monkeypatch, False)

        warning = self._warning(backend)

        assert warning is not None
        assert warning["active"] == "kiwipiepy"
        assert "will fall back" in warning["detail"]

    def test_the_fix_names_the_extra_the_restart_and_the_rebuild(self, monkeypatch, backend):
        _fall_back(monkeypatch, backend)
        self._installed(monkeypatch, False)

        fix = self._warning(backend)["fix"]

        assert "memtomem[onnx,korean]" in fix
        assert "restart" in fix
        # A bare ``mm`` can lack the extra (it would rebuild as unicode61), be
        # absent on a uvx-only install, or open another store — so the rebuild
        # runs through the server's own launcher rather than a pinned command
        # (a pinned version also fails for an unpublished dev build).
        rebuild = "mm config set search.tokenizer kiwipiepy"
        assert rebuild in fix
        assert "launcher and environment" in fix
        assert "uvx --from" not in fix and __version__ not in fix
        assert fix.index("restart") < fix.index(rebuild), (
            "rebuilding before every writer restarts lets a live fallback "
            "server write unicode61 rows into the rebuilt index"
        )

    def test_mm_status_reports_it(self, monkeypatch, backend):
        _fall_back(monkeypatch, backend)
        self._installed(monkeypatch, False)
        comp = SimpleNamespace(
            config=Mem2MemConfig(search={"tokenizer": "kiwipiepy"}),
            storage=SimpleNamespace(
                get_stats=AsyncMock(return_value={"total_chunks": 1, "total_sources": 1}),
                get_all_source_files=AsyncMock(return_value=[]),
                stored_embedding_info=None,
                embedding_mismatch=None,
                fts_tokenizer_effective=backend.fts_tokenizer_effective,
            ),
            embedder=SimpleNamespace(),
        )

        @asynccontextmanager
        async def fake():
            yield comp

        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", fake)
        runner = CliRunner()

        result = runner.invoke(cli, ["status", "--json"])
        assert result.exit_code == 0, result.output
        warnings = json.loads(result.output)["warnings"]
        (warning,) = [w for w in warnings if w["kind"] == "tokenizer_fallback"]
        assert set(warning) == {"kind", "configured", "active", "detail", "fix"}

        text = runner.invoke(cli, ["status"])
        assert text.exit_code == 0, text.output
        assert "- kind:       tokenizer_fallback" in text.output


class TestConfigRebuildMessage:
    """``mem_config`` names the tokenizer the rebuilt rows actually used (#3385)."""

    @pytest.fixture(autouse=True)
    def _restore(self, restore_tokenizer):
        pass

    @pytest.fixture
    async def config_app(self, tmp_path, monkeypatch):
        storage = SqliteBackend(StorageConfig(sqlite_path=tmp_path / "config.db"))
        await storage.initialize()
        await storage.upsert_chunks([make_chunk(_ROW_A, source="a.md", embedding=[0.1] * 768)])
        config = Mem2MemConfig()
        app = SimpleNamespace(
            config=config,
            search_pipeline=SimpleNamespace(invalidate_cache=lambda: None, config=config.search),
            storage=storage,
            embedder=SimpleNamespace(config=config.embedding),
        )
        monkeypatch.setattr(status_config, "_get_app_initialized", AsyncMock(return_value=app))
        yield app
        await storage.close()

    async def test_the_reply_names_kiwipiepy_when_the_rebuild_used_it(
        self, config_app, monkeypatch
    ):
        pytest.importorskip("kiwipiepy", reason="the korean extra is not installed")
        _use_kiwi(monkeypatch, config_app.storage)

        reply = await status_config.mem_config("search.tokenizer", "kiwipiepy")

        assert "FTS index rebuilt with kiwipiepy (1 chunks)." in reply
        assert "warning:" not in reply

    async def test_the_reply_names_the_fallback_and_warns_about_it(self, config_app, monkeypatch):
        _fall_back(monkeypatch, config_app.storage)

        reply = await status_config.mem_config("search.tokenizer", "kiwipiepy")

        assert "FTS index rebuilt with unicode61 (1 chunks)." in reply
        assert "kiwipiepy is unavailable" in reply
        assert "fall back to unicode61" in reply


def _block_kiwi(monkeypatch) -> None:
    """A process without the extra whose fallback has not tripped yet.

    Unlike ``_fall_back`` nothing tokenizes here, so the next kiwipiepy call
    (the rebuild under test) is the one that falls back. A read-back placed
    before the rebuild would still see ``kiwipiepy``.
    """
    monkeypatch.setitem(sys.modules, "kiwipiepy", None)
    monkeypatch.setattr(fts_tokenizer, "_kiwi_instance", None)
    monkeypatch.setattr(fts_tokenizer, "_kiwi_unavailable", False)


class TestWebRebuildReport:
    """The web's FTS rebuilds name the tokenizer the rows were built with (#3395)."""

    @pytest.fixture(autouse=True)
    def _restore(self, restore_tokenizer):
        pass

    @pytest.fixture
    async def web(self, tmp_path, monkeypatch):
        from httpx import ASGITransport, AsyncClient

        from memtomem.web import hot_reload
        from memtomem.web.app import create_app
        from memtomem.web.deps import require_configured

        set_home(monkeypatch, tmp_path / "home")
        for k in list(os.environ):
            if k.startswith("MEMTOMEM_"):
                monkeypatch.delenv(k, raising=False)
        # Written as unicode61, so only the rebuild under test can trip a fallback.
        storage = SqliteBackend(
            StorageConfig(sqlite_path=tmp_path / "web.db"), fts_tokenizer="unicode61"
        )
        await storage.initialize()
        await storage.upsert_chunks([make_chunk(_ROW_A, source="a.md", embedding=[0.1] * 768)])
        app = create_app(lifespan=None, mode="dev")
        app.state.csrf_enforce = False
        app.dependency_overrides[require_configured] = lambda: None
        app.state.storage = storage
        app.state.search_pipeline = SimpleNamespace(invalidate_cache=lambda: None)
        app.state.config = hot_reload._build_fresh_config()
        app.state.config_signature = hot_reload.current_signature()
        app.state.last_reload_error = None
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            yield SimpleNamespace(app=app, client=client, storage=storage)
        await storage.close()

    async def test_the_rebuild_button_names_kiwipiepy_when_the_rebuild_used_it(
        self, web, monkeypatch
    ):
        pytest.importorskip("kiwipiepy", reason="the korean extra is not installed")
        _use_kiwi(monkeypatch, web.storage)

        resp = await web.client.post("/api/fts-rebuild")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["tokenizer"] == "kiwipiepy"
        assert body["message"] == "FTS index rebuilt with kiwipiepy (1 chunks)."
        assert body["warning"] is None

    async def test_the_rebuild_button_warns_when_the_rebuild_itself_falls_back(
        self, web, monkeypatch
    ):
        _block_kiwi(monkeypatch)
        web.storage.set_fts_tokenizer("kiwipiepy")
        assert web.storage.fts_tokenizer_effective == "kiwipiepy", "the fallback tripped early"

        resp = await web.client.post("/api/fts-rebuild")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["tokenizer"] == "unicode61"
        assert "kiwipiepy is unavailable" in body["warning"]
        assert "fall back to unicode61" in body["warning"]
        # The UI shows ``message`` alone, so it has to carry the warning.
        assert body["message"] == f"FTS index rebuilt with unicode61 (1 chunks). {body['warning']}"

    async def test_a_config_save_that_falls_back_logs_a_warning(self, web, monkeypatch, caplog):
        _block_kiwi(monkeypatch)

        with caplog.at_level(logging.INFO, logger="memtomem.web.routes.system"):
            resp = await web.client.patch(
                "/api/config", json={"search": {"tokenizer": "kiwipiepy"}}
            )

        assert resp.status_code == 200, resp.text
        assert [c["field"] for c in resp.json()["applied"]] == ["search.tokenizer"]
        rebuilt = [r for r in caplog.records if r.getMessage().startswith("FTS index rebuilt")]
        assert [(r.levelno, r.getMessage().split(":")[0]) for r in rebuilt] == [
            (logging.WARNING, "FTS index rebuilt with tokenizer=unicode61 (1 chunks)")
        ]
        assert "kiwipiepy is unavailable" in rebuilt[0].getMessage()

    async def test_a_hot_reload_rebuild_that_falls_back_logs_a_warning(
        self, web, monkeypatch, caplog
    ):
        from memtomem.web import hot_reload

        _block_kiwi(monkeypatch)
        web.storage.set_fts_tokenizer("kiwipiepy")

        with caplog.at_level(logging.INFO, logger=hot_reload.logger.name):
            await hot_reload._schedule_fts_rebuild(web.storage, "kiwipiepy")

        rebuilt = [r for r in caplog.records if r.getMessage().startswith("FTS index rebuilt")]
        assert [(r.levelno, r.getMessage().split(":")[0]) for r in rebuilt] == [
            (logging.WARNING, "FTS index rebuilt with tokenizer=unicode61 (1 chunks)")
        ]


class _SupersededStorage:
    """A backend whose tokenizer changes while a queued rebuild runs.

    ``rebuild_fts`` retries with the backend's live tokenizer when it changes
    mid-pass, so the pass that was queued for one tokenizer commits with the
    next. This double does that switch from inside the rebuild.
    """

    def __init__(self, queued: str, live: str) -> None:
        self._tokenizer = queued
        self._live = live

    @property
    def fts_tokenizer(self) -> str:
        return self._tokenizer

    @property
    def fts_tokenizer_effective(self) -> str:
        return fts_tokenizer.effective_tokenizer(self._tokenizer)

    def set_fts_tokenizer(self, name: str) -> None:
        self._tokenizer = name

    async def rebuild_fts(self) -> int:
        self.set_fts_tokenizer(self._live)
        return 1


class TestHotReloadComparesTheLiveTokenizer:
    """A coalesced rebuild is judged by the backend, not by the value it was queued for."""

    @pytest.fixture(autouse=True)
    def _restore(self, restore_tokenizer):
        pass

    async def _rebuild(self, storage, queued: str, caplog) -> list[logging.LogRecord]:
        from memtomem.web import hot_reload

        app = SimpleNamespace(state=SimpleNamespace())
        with caplog.at_level(logging.INFO, logger=hot_reload.logger.name):
            await hot_reload._schedule_fts_rebuild(storage, queued, app=app)
            await app.state.fts_rebuild_task
        return [r for r in caplog.records if r.getMessage().startswith("FTS index rebuilt")]

    async def test_a_queued_kiwipiepy_pass_that_ran_as_unicode61_does_not_warn(
        self, monkeypatch, caplog
    ):
        monkeypatch.setattr(fts_tokenizer, "_kiwi_unavailable", True)
        storage = _SupersededStorage(queued="kiwipiepy", live="unicode61")

        rebuilt = await self._rebuild(storage, "kiwipiepy", caplog)

        assert [(r.levelno, r.getMessage()) for r in rebuilt] == [
            (logging.INFO, "FTS index rebuilt with tokenizer=unicode61 (1 chunks)")
        ]

    async def test_a_queued_unicode61_pass_that_ran_as_kiwipiepy_warns(self, monkeypatch, caplog):
        monkeypatch.setattr(fts_tokenizer, "_kiwi_unavailable", True)
        storage = _SupersededStorage(queued="unicode61", live="kiwipiepy")

        rebuilt = await self._rebuild(storage, "unicode61", caplog)

        assert [r.levelno for r in rebuilt] == [logging.WARNING]
        assert "kiwipiepy is unavailable" in rebuilt[0].getMessage()
