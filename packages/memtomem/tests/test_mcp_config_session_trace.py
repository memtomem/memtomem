"""MCP neither changes nor persists ``session_trace``; the CLI and web still do.

``session_trace`` decides whether the session CLI records traces, how much of
each payload they carry and where they are sent or written. An MCP caller is an
agent that may be acting on instructions found in its input, so it may read
these settings but not set them, and its saves of other keys must not write the
server's (possibly stale) copy of them back over the file.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from memtomem import config as cfg_mod
from memtomem.config import MUTABLE_FIELDS, Mem2MemConfig
from memtomem.config_signature import build_fresh_config
from memtomem.server.helpers import _set_config_key, mcp_saved_fields
from memtomem.server.tools import meta, status_config

from .helpers import isolate_config_paths, set_home

TRACE_FIELDS = sorted(MUTABLE_FIELDS["session_trace"])

# A value each field would accept, so a refusal cannot come from coercion.
VALID_VALUE = {
    "enabled": "true",
    "jsonl_enabled": "true",
    "jsonl_path": "/tmp/elsewhere/out.jsonl",
    "langfuse_enabled": "false",
    "langfuse_public_key": "pk-SENTINEL-4f1c",
    "langfuse_secret_key": "sk-SENTINEL-4f1c",
    "langfuse_host": "https://collector.invalid",
    "sampling_rate": "0.5",
    "payload_mode": "full",
    "max_payload_chars": "123",
}


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    return isolate_config_paths(monkeypatch, tmp_path / "config-home") / "config.json"


def effective(trace) -> dict:
    """``trace`` as the session CLI acts on it.

    Every save stores a path under HOME as ``~/...``
    (``_relativize_config_paths_in_place``), and the JSONL sink expands it
    before use, so the spelling may change while the file written does not.
    """
    data = trace.model_dump()
    data["jsonl_path"] = data["jsonl_path"].expanduser()
    return data


def install_app(monkeypatch, config):
    pipeline = SimpleNamespace(
        invalidate_cache=MagicMock(),
        config=config.search,
        rerank_active=False,
        swap_reranker=AsyncMock(),
    )
    app = SimpleNamespace(
        config=config,
        search_pipeline=pipeline,
        storage=SimpleNamespace(rebuild_fts=AsyncMock(), set_fts_tokenizer=MagicMock()),
        embedder=SimpleNamespace(config=config.embedding),
        _config_lock=asyncio.Lock(),
        _rerank_toggle_lock=asyncio.Lock(),
    )
    monkeypatch.setattr(status_config, "_get_app_initialized", AsyncMock(return_value=app))
    return app


def test_value_table_covers_every_trace_field():
    assert sorted(VALID_VALUE) == TRACE_FIELDS


def test_cli_and_web_keep_every_trace_field():
    # ``mm config set`` and ``PATCH /api/config`` read MUTABLE_FIELDS; the MCP
    # refusal is a separate set, so the section must stay listed here.
    assert set(TRACE_FIELDS) == {
        "enabled",
        "jsonl_enabled",
        "jsonl_path",
        "langfuse_enabled",
        "langfuse_public_key",
        "langfuse_secret_key",
        "langfuse_host",
        "sampling_rate",
        "payload_mode",
        "max_payload_chars",
    }


def test_mcp_saves_every_other_section():
    saved = mcp_saved_fields()

    assert "session_trace" not in saved
    assert saved == {s: f for s, f in MUTABLE_FIELDS.items() if s != "session_trace"}


@pytest.mark.parametrize("field", TRACE_FIELDS)
def test_setter_refuses_every_trace_field(field):
    config = Mem2MemConfig()
    section = config.session_trace
    before = section.model_dump()
    fields_set = section.model_fields_set.copy()
    key = f"session_trace.{field}"
    value = VALID_VALUE[field]

    msg = _set_config_key(config, key, value)

    assert not msg.startswith("Set ")
    assert "cannot be changed through MCP" in msg
    assert f"`mm config set {key} <value>`" in msg
    assert value not in msg
    assert config.session_trace is section
    assert section.model_dump() == before
    assert section.model_fields_set == fields_set


@pytest.mark.parametrize("route", ["mem_config", "mem_do"])
@pytest.mark.parametrize("field", TRACE_FIELDS)
async def test_tool_refuses_without_saving_or_fanout(route, field, config_path, monkeypatch):
    config_path.write_text(json.dumps({"search": {"default_top_k": 9}}), encoding="utf-8")
    disk_before = config_path.read_bytes()
    config = Mem2MemConfig()
    app = install_app(monkeypatch, config)
    before = config.model_dump()
    save = MagicMock()
    monkeypatch.setattr(cfg_mod, "save_config_overrides", save)
    key, value = f"session_trace.{field}", VALID_VALUE[field]

    if route == "mem_config":
        msg = await status_config.mem_config(key, value, persist=True)
    else:
        msg = await meta.mem_do("config", {"key": key, "value": value, "persist": True})

    assert "cannot be changed through MCP" in msg
    assert value not in msg
    assert config.model_dump() == before
    save.assert_not_called()
    assert config_path.read_bytes() == disk_before
    app.search_pipeline.invalidate_cache.assert_not_called()


@pytest.mark.parametrize("route", ["mem_config", "mem_do", "rerank.enabled"])
async def test_unrelated_save_keeps_the_files_trace_settings(
    route, tmp_path, config_path, monkeypatch
):
    home = tmp_path / "home"
    home.mkdir()
    set_home(monkeypatch, home)
    trace_path = str(home / "logs" / "trace.jsonl")
    # The server starts while tracing is on with full payloads...
    config_path.write_text(
        json.dumps(
            {
                "session_trace": {
                    "enabled": True,
                    "payload_mode": "full",
                    "jsonl_path": trace_path,
                },
                "rerank": {"enabled": True},
            }
        ),
        encoding="utf-8",
    )
    config = build_fresh_config(migrate=False)
    assert config.session_trace.enabled is True
    app = install_app(monkeypatch, config)
    # ...then the user turns it off from a terminal while the server runs.
    config_path.write_text(
        json.dumps({"session_trace": {"jsonl_path": trace_path}, "rerank": {"enabled": True}}),
        encoding="utf-8",
    )
    on_disk = effective(build_fresh_config(migrate=False).session_trace)
    assert on_disk["enabled"] is False
    assert on_disk["payload_mode"] == "metadata"

    if route == "mem_config":
        msg = await status_config.mem_config("search.default_top_k", "17", persist=True)
    elif route == "mem_do":
        msg = await meta.mem_do(
            "config", {"key": "search.default_top_k", "value": "17", "persist": True}
        )
    else:
        msg = await status_config.mem_config("rerank.enabled", "false", persist=True)

    assert msg.startswith("Set "), msg
    reloaded = build_fresh_config(migrate=False)
    assert effective(reloaded.session_trace) == on_disk
    if route == "rerank.enabled":
        assert reloaded.rerank.enabled is False
    else:
        assert reloaded.search.default_top_k == 17
        assert json.loads(config_path.read_text(encoding="utf-8"))["search"] == {
            "default_top_k": 17
        }
    # The running server's own copy is not touched by the save.
    assert app.config.session_trace.enabled is True
