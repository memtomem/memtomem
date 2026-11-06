"""``mem_config(rerank.enabled)`` changes what the running pipeline does (#3098).

The pipeline reranks iff it holds a reranker instance, so a config flip alone
left it sending queries to a remote reranker after "off" (while ``mem_status``
said off) and never reranking after "on".
"""

from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from memtomem import config as cfg_mod
from memtomem.config import Mem2MemConfig
from memtomem.generation import ComponentGeneration
from memtomem.runtime.components import create_search_pipeline
from memtomem.search.reranker import factory as rr_factory
from memtomem.server.tools import meta, status_config

from .helpers import isolate_config_paths


class FakeReranker:
    def __init__(self, config, *, load_gate=None, load_started=None, load_error=None):
        self.config = config
        self.closed = 0
        self.loads = 0
        self._load_gate = load_gate
        self._load_started = load_started
        self._load_error = load_error
        self.close_gate: asyncio.Event | None = None
        self.close_started = asyncio.Event()

    def _get_model(self):
        self.loads += 1
        if self._load_started is not None:
            self._load_started.set()
        if self._load_gate is not None:
            assert self._load_gate.wait(10), "load gate never released"
        if self._load_error is not None:
            raise self._load_error

    async def close(self):
        self.close_started.set()
        if self.close_gate is not None:
            await self.close_gate.wait()
        self.closed += 1


class Factory:
    """``create_reranker`` stand-in that records every candidate it builds."""

    def __init__(self):
        self.built: list[FakeReranker] = []
        self.load_gate: threading.Event | None = None
        self.load_started = threading.Event()
        self.load_error: Exception | None = None
        self.build_error: Exception | None = None

    def __call__(self, config):
        if self.build_error is not None:
            raise self.build_error
        if not config.enabled:
            return None
        r = FakeReranker(
            config,
            load_gate=self.load_gate,
            load_started=self.load_started,
            load_error=self.load_error,
        )
        self.built.append(r)
        return r


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    return isolate_config_paths(monkeypatch, tmp_path / "config-home") / "config.json"


@pytest.fixture
def factory(monkeypatch):
    f = Factory()
    monkeypatch.setattr(rr_factory, "create_reranker", f)
    return f


def build_pipeline(config, reranker):
    return create_search_pipeline(
        config,
        storage=MagicMock(),
        embedder=MagicMock(),
        reranker=reranker,
        llm_provider=None,
        generation=ComponentGeneration(),
    )


def install_app(monkeypatch, *, enabled: bool, active: bool, provider: str = "fastembed"):
    """An app whose config says *enabled* and whose pipeline reranks iff *active*."""
    config = Mem2MemConfig()
    config.rerank.provider = provider
    config.rerank.enabled = enabled
    original = FakeReranker(config.rerank) if active else None
    app = SimpleNamespace(
        config=config,
        search_pipeline=build_pipeline(config, original),
        storage=SimpleNamespace(rebuild_fts=AsyncMock(), set_fts_tokenizer=MagicMock()),
        embedder=MagicMock(),
        _config_lock=asyncio.Lock(),
        _rerank_toggle_lock=asyncio.Lock(),
    )
    app.original = original
    monkeypatch.setattr(status_config, "_get_app_initialized", AsyncMock(return_value=app))
    return app


def agree(app) -> bool:
    return app.config.rerank.enabled == app.search_pipeline.rerank_active


async def set_enabled(value: str, **kw) -> str:
    return await status_config.mem_config("rerank.enabled", value, **kw)


# --- basic toggles ---------------------------------------------------------


async def test_enable_installs_a_loaded_reranker(monkeypatch, config_path, factory):
    app = install_app(monkeypatch, enabled=False, active=False)

    msg = await set_enabled("true")

    assert msg.startswith("Set rerank.enabled = True")
    assert msg.endswith("Reranking is now on for new searches on this server.")
    [built] = factory.built
    assert built.loads == 1
    assert app.search_pipeline._reranker is built
    # By identity, so later pool-knob edits keep reaching the pipeline.
    assert app.search_pipeline._rerank_config is app.config.rerank
    assert agree(app)


async def test_disable_retires_and_closes_the_reranker(monkeypatch, config_path, factory):
    app = install_app(monkeypatch, enabled=True, active=True)

    msg = await set_enabled("false")

    assert msg.endswith("Reranking is now off for new searches on this server.")
    assert not app.search_pipeline.rerank_active
    assert app.original.closed == 1
    assert factory.built == []
    assert agree(app)


async def test_disable_with_a_search_in_flight_defers_the_close(monkeypatch, config_path, factory):
    app = install_app(monkeypatch, enabled=True, active=True)
    pipeline = app.search_pipeline

    with pipeline._lease_reranker() as (leased, _cfg):
        await set_enabled("false")
        assert not pipeline.rerank_active
        assert leased is app.original
        assert app.original.closed == 0
    await asyncio.gather(*list(pipeline._bg_tasks))
    assert app.original.closed == 1


async def test_already_on_builds_nothing(monkeypatch, config_path, factory):
    app = install_app(monkeypatch, enabled=True, active=True)

    msg = await set_enabled("true")

    assert "Reranking is now" not in msg
    assert factory.built == []
    assert app.search_pipeline._reranker is app.original


async def test_pool_knob_edit_after_enable_reaches_the_pipeline(monkeypatch, config_path, factory):
    app = install_app(monkeypatch, enabled=False, active=False)
    await set_enabled("true")

    assert (await status_config.mem_config("rerank.oversample", "3.5")).startswith("Set ")

    assert app.search_pipeline._rerank_config.oversample == 3.5


async def test_mem_do_reaches_the_same_path(monkeypatch, config_path, factory):
    app = install_app(monkeypatch, enabled=True, active=True)

    msg = await meta.mem_do("config", {"key": "rerank.enabled", "value": "false"})

    assert "Reranking is now off" in msg
    assert not app.search_pipeline.rerank_active


# --- decided by what the pipeline does, not by the old config value ---------


async def test_config_on_but_pipeline_off_is_repaired_by_asking_for_on(
    monkeypatch, config_path, factory
):
    app = install_app(monkeypatch, enabled=True, active=False)

    msg = await set_enabled("true")

    assert "Reranking is now on" in msg
    assert app.search_pipeline.rerank_active
    assert agree(app)


async def test_config_off_but_pipeline_on_is_repaired_by_asking_for_off(
    monkeypatch, config_path, factory
):
    app = install_app(monkeypatch, enabled=False, active=True)

    msg = await set_enabled("false")

    assert "Reranking is now off" in msg
    assert not app.search_pipeline.rerank_active
    assert app.original.closed == 1


# --- failures leave config and pipeline as they were ------------------------


@pytest.mark.parametrize("where", ["build", "load"])
@pytest.mark.parametrize("enabled", [False, True], ids=["matching", "mismatched"])
async def test_failed_enable_changes_nothing(monkeypatch, config_path, factory, where, enabled):
    app = install_app(monkeypatch, enabled=enabled, active=False)
    fields_set = app.config.rerank.model_fields_set.copy()
    if where == "build":
        factory.build_error = RuntimeError("no such provider")
    else:
        factory.load_error = RuntimeError("fastembed is not installed")
    save = MagicMock()
    monkeypatch.setattr(cfg_mod, "save_config_overrides", save)

    msg = await set_enabled("true", persist=True)

    assert msg.startswith("Cannot enable reranking: ")
    assert msg.endswith("Nothing was changed.")
    assert app.config.rerank.enabled is enabled
    assert app.config.rerank.model_fields_set == fields_set
    assert not app.search_pipeline.rerank_active
    assert all(r.closed == 1 for r in factory.built)
    save.assert_not_called()


@pytest.mark.parametrize("failure", [TimeoutError, ValueError])
@pytest.mark.parametrize("direction", ["enable", "disable"])
async def test_failed_persist_rolls_back_and_keeps_the_pipeline(
    monkeypatch, config_path, factory, failure, direction
):
    enabled = direction == "disable"
    app = install_app(monkeypatch, enabled=enabled, active=enabled)
    before = app.search_pipeline._reranker
    fields_set = app.config.rerank.model_fields_set.copy()
    monkeypatch.setattr(
        cfg_mod, "save_config_overrides", MagicMock(side_effect=failure("injected"))
    )

    msg = await set_enabled("false" if enabled else "true", persist=True)

    assert "rolled back" in msg
    assert app.config.rerank.enabled is enabled
    assert app.config.rerank.model_fields_set == fields_set
    assert app.search_pipeline._reranker is before
    assert all(r.closed == 1 for r in factory.built)
    if app.original is not None:
        assert app.original.closed == 0


async def test_invalid_value_is_refused_before_any_build(monkeypatch, config_path, factory):
    app = install_app(monkeypatch, enabled=False, active=False)

    msg = await set_enabled("maybe")

    assert msg.startswith("Invalid value 'maybe' for 'rerank.enabled'")
    assert factory.built == []
    assert app.config.rerank.enabled is False


# --- what happens while the model loads -----------------------------------


async def start_blocked_enable(factory, **kw):
    factory.load_gate = threading.Event()
    task = asyncio.create_task(set_enabled("true", **kw))
    assert await asyncio.to_thread(factory.load_started.wait, 10)
    return task


async def test_live_config_is_untouched_while_the_model_loads(monkeypatch, config_path, factory):
    app = install_app(monkeypatch, enabled=False, active=False)
    task = await start_blocked_enable(factory)
    try:
        assert app.config.rerank.enabled is False
        assert status_config._inference_status(app.config)["rerank"]["enabled"] is False
        # A concurrent edit of another rerank field lands and survives the commit.
        assert (await status_config.mem_config("rerank.oversample", "4.0")).startswith("Set ")
        # A concurrent persist of an unrelated key must not save the pending toggle.
        assert (
            await status_config.mem_config("search.default_top_k", "7", persist=True)
        ).startswith("Set ")
        saved = json.loads(config_path.read_text(encoding="utf-8"))
        assert saved.get("rerank", {}).get("enabled") is not True
    finally:
        factory.load_gate.set()
    assert "Reranking is now on" in await task
    assert app.config.rerank.oversample == 4.0
    assert app.search_pipeline.rerank_active


async def test_cancelled_load_closes_the_candidate(monkeypatch, config_path, factory):
    app = install_app(monkeypatch, enabled=False, active=False)
    task = await start_blocked_enable(factory)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    factory.load_gate.set()

    [candidate] = factory.built
    assert candidate.closed == 1
    assert app.config.rerank.enabled is False
    assert not app.search_pipeline.rerank_active


async def test_toggles_are_serialized_so_the_last_call_wins(monkeypatch, config_path, factory):
    app = install_app(monkeypatch, enabled=False, active=False)
    enable = await start_blocked_enable(factory)
    disable = asyncio.create_task(set_enabled("false"))
    await asyncio.sleep(0.05)
    assert not disable.done()

    factory.load_gate.set()
    await enable
    await disable

    assert app.config.rerank.enabled is False
    assert not app.search_pipeline.rerank_active
    assert factory.built[0].closed == 1


@pytest.mark.parametrize("replacement_active", [False, True])
async def test_commit_acts_on_a_pipeline_replaced_during_the_load(
    monkeypatch, config_path, factory, replacement_active
):
    app = install_app(monkeypatch, enabled=False, active=False)
    task = await start_blocked_enable(factory)
    replacement_reranker = FakeReranker(app.config.rerank) if replacement_active else None
    old_pipeline = app.search_pipeline
    app.search_pipeline = build_pipeline(app.config, replacement_reranker)
    factory.load_gate.set()

    await task

    [candidate] = factory.built
    assert not old_pipeline.rerank_active
    if replacement_active:
        assert app.search_pipeline._reranker is replacement_reranker
        assert candidate.closed == 1
    else:
        assert app.search_pipeline._reranker is candidate
        assert candidate.closed == 0
    assert agree(app)


# --- retirement is committed once published ---------------------------------


async def test_cancelling_during_retirement_still_closes_the_old_reranker(
    monkeypatch, config_path, factory
):
    app = install_app(monkeypatch, enabled=True, active=True)
    pipeline = app.search_pipeline
    app.original.close_gate = asyncio.Event()
    task = asyncio.create_task(set_enabled("false"))
    await app.original.close_started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not pipeline.rerank_active
    assert pipeline._bg_tasks

    app.original.close_gate.set()
    await pipeline.close()
    assert app.original.closed == 1


# --- mem_status describes what the pipeline does ----------------------------


async def test_status_remote_endpoint_follows_the_pipeline(monkeypatch, config_path, factory):
    app = install_app(monkeypatch, enabled=True, active=True, provider="cohere")

    def listed() -> bool:
        inference = status_config._inference_status(app.config)
        row_on = inference["rerank"]["enabled"]
        endpoint = any(ep["component"] == "rerank" for ep in inference["remote_endpoints"])
        assert row_on is endpoint
        return endpoint

    assert listed() and app.search_pipeline.rerank_active
    await set_enabled("false")
    assert not listed() and not app.search_pipeline.rerank_active
    await set_enabled("true")
    assert listed() and app.search_pipeline.rerank_active
