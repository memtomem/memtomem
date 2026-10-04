"""A ``kiwipiepy`` store tokenized as ``unicode61`` says so in ``mem_status`` (#2647).

Every plugin launcher installs ``memtomem[onnx]``, so a server sharing a store
configured with the ``korean`` preset has no ``kiwipiepy``. The tokenizer then
switches that process to ``unicode61`` on first use and keeps writing: FTS
content is pre-tokenized in Python, so one index ends up holding two token
representations and Korean keyword queries miss rows across them.
``TestMixedStoreRecall`` pins the damage and the recovery the warning's ``fix``
names; ``TestFallbackWarning`` pins the warning itself.
"""

from __future__ import annotations

import json
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from click.testing import CliRunner
from helpers import make_chunk

from memtomem import __version__
from memtomem.cli import cli
from memtomem.config import Mem2MemConfig
from memtomem.server.tools import status_config
from memtomem.server.tools.status_config import _tokenizer_fallback_warning
from memtomem.storage import fts_tokenizer

_ROW_A = "마케팅 예산을 삭감하기로 결정했다"
_ROW_B = "영업 예산을 동결하기로 결정했다"


@pytest.fixture
def restore_tokenizer(monkeypatch):
    """The active tokenizer is process-global; hand it back as found."""
    previous = fts_tokenizer.get_tokenizer()
    yield
    monkeypatch.delitem(sys.modules, "kiwipiepy", raising=False)
    fts_tokenizer.set_tokenizer(previous)


def _fall_back(monkeypatch) -> None:
    """Configure kiwipiepy with the import blocked, and trip the fallback."""
    monkeypatch.setitem(sys.modules, "kiwipiepy", None)
    fts_tokenizer.set_tokenizer("kiwipiepy")
    fts_tokenizer.tokenize_for_fts("워밍업")
    assert fts_tokenizer.get_tokenizer() == "unicode61", "the fallback did not trip"


def _use_kiwi(monkeypatch) -> None:
    monkeypatch.delitem(sys.modules, "kiwipiepy", raising=False)
    fts_tokenizer.set_tokenizer("kiwipiepy")


class TestMixedStoreRecall:
    @pytest.fixture(autouse=True)
    def _kiwi(self, restore_tokenizer):
        pytest.importorskip("kiwipiepy", reason="the korean extra is not installed")

    async def _mixed_store(self, storage, monkeypatch) -> dict[str, str]:
        _use_kiwi(monkeypatch)
        a = make_chunk(_ROW_A, source="a.md")
        await storage.upsert_chunks([a])
        _fall_back(monkeypatch)
        b = make_chunk(_ROW_B, source="b.md")
        await storage.upsert_chunks([b])
        return {a.id: "a", b.id: "b"}

    async def _found(self, storage, ids, query) -> list[str]:
        return sorted(ids[r.chunk.id] for r in await storage.bm25_search(query, top_k=10))

    @pytest.mark.asyncio
    async def test_an_inflected_query_misses_rows_written_the_other_way(self, storage, monkeypatch):
        ids = await self._mixed_store(storage, monkeypatch)

        _use_kiwi(monkeypatch)
        assert await self._found(storage, ids, "예산을") == ["a"]
        _fall_back(monkeypatch)
        assert await self._found(storage, ids, "예산을") == ["b"]
        # A bare stem still reaches both through unicode61's prefix ``*``,
        # which is why casual testing does not show the split.
        assert await self._found(storage, ids, "예산") == ["a", "b"]

    @pytest.mark.asyncio
    async def test_a_rebuild_under_kiwipiepy_restores_both_rows(self, storage, monkeypatch):
        ids = await self._mixed_store(storage, monkeypatch)

        _use_kiwi(monkeypatch)
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

    def test_a_unicode61_store_has_nothing_to_warn_about(self, monkeypatch):
        self._installed(monkeypatch, False)
        fts_tokenizer.set_tokenizer("unicode61")

        assert _tokenizer_fallback_warning("unicode61") is None

    def test_a_working_kiwipiepy_store_has_nothing_to_warn_about(self, monkeypatch):
        self._installed(monkeypatch, True)
        fts_tokenizer.set_tokenizer("kiwipiepy")

        assert _tokenizer_fallback_warning("kiwipiepy") is None

    def test_a_tripped_fallback_warns_on_every_call(self, monkeypatch):
        _fall_back(monkeypatch)
        self._installed(monkeypatch, False)

        first = _tokenizer_fallback_warning("kiwipiepy")
        second = _tokenizer_fallback_warning("kiwipiepy")

        assert first is not None and first == second
        assert first["kind"] == "tokenizer_fallback"
        assert (first["configured"], first["active"]) == ("kiwipiepy", "unicode61")

    def test_installing_the_module_later_does_not_clear_it(self, monkeypatch):
        """The fallback is sticky per process: only a restart undoes it."""
        _fall_back(monkeypatch)
        self._installed(monkeypatch, True)

        warning = _tokenizer_fallback_warning("kiwipiepy")

        assert warning is not None
        assert warning["active"] == "unicode61"

    def test_a_pending_fallback_warns_before_the_first_tokenize(self, monkeypatch):
        """Status never tokenizes, so a missing module has not tripped it yet."""
        fts_tokenizer.set_tokenizer("kiwipiepy")
        self._installed(monkeypatch, False)

        warning = _tokenizer_fallback_warning("kiwipiepy")

        assert warning is not None
        assert warning["active"] == "kiwipiepy"
        assert "will fall back" in warning["detail"]

    def test_the_fix_names_the_extra_the_restart_and_the_rebuild(self, monkeypatch):
        _fall_back(monkeypatch)
        self._installed(monkeypatch, False)

        fix = _tokenizer_fallback_warning("kiwipiepy")["fix"]

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

    def test_mm_status_reports_it(self, monkeypatch):
        _fall_back(monkeypatch)
        self._installed(monkeypatch, False)
        comp = SimpleNamespace(
            config=Mem2MemConfig(search={"tokenizer": "kiwipiepy"}),
            storage=SimpleNamespace(
                get_stats=AsyncMock(return_value={"total_chunks": 1, "total_sources": 1}),
                get_all_source_files=AsyncMock(return_value=[]),
                stored_embedding_info=None,
                embedding_mismatch=None,
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
