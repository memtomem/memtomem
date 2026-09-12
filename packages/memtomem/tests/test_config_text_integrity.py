"""#2411: display escaping must not change the values next to the display."""

from __future__ import annotations

import copy
import json
import logging
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import click
import pytest
from click.testing import CliRunner

from memtomem import config as cfg_mod
from memtomem._runtime_paths import scrub_text
from memtomem.cli import cli
from memtomem.config import Mem2MemConfig
from memtomem.errors import ConfigFragmentError
from memtomem.server.tools import status_config

from helpers import set_home


HOSTILE = "name\nFORGED\r\x1b[2J\x07"
ESCAPED = r"name\x0aFORGED\x0d\x1b[2J\x07"


@pytest.fixture
def config_home(tmp_path, monkeypatch):
    set_home(monkeypatch, tmp_path)
    for name in list(os.environ):
        if name.upper().startswith("MEMTOMEM_"):
            monkeypatch.delenv(name, raising=False)
    root = tmp_path / ".memtomem"
    root.mkdir()
    return root


@pytest.mark.parametrize("value", [HOSTILE, "한국어 모델", "name\x85\u2028end", "name\ud800end"])
async def test_mcp_text_escapes_without_changing_config_or_store(config_home, monkeypatch, value):
    config = Mem2MemConfig()
    config.embedding.provider = value
    config.embedding.model = value
    stored = {"provider": value, "model": value, "dimension": 384}
    mismatch = {"stored": stored, "configured": dict(stored)}
    storage = SimpleNamespace(
        stored_embedding_info=stored,
        embedding_mismatch=mismatch,
        get_stats=AsyncMock(return_value={"total_chunks": 0, "total_sources": 0}),
        get_all_source_files=AsyncMock(return_value=[]),
    )
    app = SimpleNamespace(config=config, storage=storage)
    monkeypatch.setattr(status_config, "_get_app_initialized", AsyncMock(return_value=app))
    monkeypatch.setattr(status_config, "_collect_concurrent_writers", lambda path: None)
    monkeypatch.setattr(
        "memtomem.server.tools.search._resolve_project_context_root", lambda app: None
    )
    before = copy.deepcopy(mismatch)
    shown = ESCAPED if value == HOSTILE else scrub_text(value)
    assert shown and shown.isprintable()

    stats = await status_config.mem_stats()
    assert stats.splitlines()[-1] == (
        f"- Embedding: DEGRADED — stored {shown}/{shown} (384d) "
        f"vs configured {shown}/{shown} (384d). "
        'Run mem_embedding_reset(mode="apply_current") to repair.'
    )
    reset = await status_config.mem_embedding_reset(mode="status")
    assert reset.splitlines()[1] == f"  DB stored:  {shown}/{shown} (384d)"
    assert reset.splitlines()[2] == f"  Config:     {shown}/{shown} (0d)"
    assert await status_config.mem_config(key="embedding.model") == f"embedding.model = {shown}"
    assert json.loads(await status_config.mem_config())["embedding"]["model"] == value
    assert config.embedding.model == value
    report = await status_config.collect_status_report(app)
    warning = next(w for w in report["warnings"] if w["kind"] == "embedding_dim_mismatch")
    assert warning["stored"] == before["stored"]
    assert warning["configured"] == before["configured"]
    assert report["config"]["embedding"]["model"] == value
    assert storage.embedding_mismatch == before
    assert storage.stored_embedding_info == before["stored"]


@pytest.mark.parametrize("color", [False, True])
@pytest.mark.parametrize("value", [HOSTILE, "한국어 모델", "name\x85\u2028end", "name\ud800end"])
def test_cli_table_and_json_keep_separate_contracts(config_home, value, color):
    path = config_home / "config.json"
    # Disable the existing legacy migration so this pin measures display only.
    path.write_text(
        json.dumps(
            {
                "embedding": {"model": value, "api_key": HOSTILE},
                "indexing": {"auto_discover": False},
            }
        )
    )
    before = path.read_bytes()
    runner = CliRunner()
    result = runner.invoke(cli, ["config", "show"], color=color)
    assert result.exit_code == 0, repr(result.output)
    shown = ESCAPED if value == HOSTILE else scrub_text(value)
    assert f"  model = {shown}" in click.unstyle(result.stdout).splitlines()
    assert "  api_key = ***" in click.unstyle(result.stdout).splitlines()
    for flag in (["--json"], ["--format", "json"]):
        result = runner.invoke(cli, ["config", "show", *flag])
        assert result.exit_code == 0, repr(result.output)
        data = json.loads(result.stdout)
        assert data["embedding"]["model"] == value
        assert data["embedding"]["api_key"] == "***"
    assert path.read_bytes() == before


def test_cli_set_escapes_both_values_but_persists_original(config_home):
    path = config_home / "config.json"
    path.write_text(json.dumps({"namespace": {"default_namespace": HOSTILE}}))
    new = HOSTILE + "-new"
    result = CliRunner().invoke(
        cli, ["config", "set", "namespace.default_namespace", new], color=True
    )
    assert result.exit_code == 0, repr(result.output)
    assert click.unstyle(result.stdout).splitlines()[0] == (
        f"namespace.default_namespace: {ESCAPED} -> {ESCAPED}-new"
    )
    assert json.loads(path.read_text())["namespace"]["default_namespace"] == new
    loaded = Mem2MemConfig()
    cfg_mod.load_config_overrides(loaded, migrate=False)
    assert loaded.namespace.default_namespace == new


def test_cli_effective_warning_and_unset_escape_shared_value(config_home, monkeypatch):
    monkeypatch.setenv("MEMTOMEM_NAMESPACE__DEFAULT_NAMESPACE", HOSTILE)
    runner = CliRunner()
    result = runner.invoke(cli, ["config", "set", "namespace.default_namespace", "new"], color=True)
    assert result.exit_code == 0, repr(result.output)
    text = click.unstyle(result.stdout)
    assert f"effective value is still {ESCAPED}." in text
    assert HOSTILE not in text
    result = runner.invoke(cli, ["config", "unset", "namespace.default_namespace"], color=True)
    assert result.exit_code == 0, repr(result.output)
    # Repeat the no-pin case: it reports the effective environment value.
    result = runner.invoke(cli, ["config", "unset", "namespace.default_namespace"], color=True)
    assert result.exit_code == 0, repr(result.output)
    assert f"supplies {ESCAPED}" in click.unstyle(result.stdout)
    assert HOSTILE not in result.stdout
    assert os.environ["MEMTOMEM_NAMESPACE__DEFAULT_NAMESPACE"] == HOSTILE


def test_cli_set_masks_secret_before_escaping(config_home, monkeypatch):
    path = config_home / "config.json"
    path.write_text(json.dumps({"session_trace": {"langfuse_secret_key": HOSTILE}}))
    monkeypatch.setenv("MEMTOMEM_SESSION_TRACE__LANGFUSE_SECRET_KEY", HOSTILE + "-env")
    result = CliRunner().invoke(
        cli, ["config", "set", "session_trace.langfuse_secret_key", HOSTILE + "-new"], color=True
    )
    assert result.exit_code == 0, repr(result.output)
    assert "session_trace.langfuse_secret_key: *** -> ***" in result.stdout
    assert "effective value is still ***." in result.stdout
    assert "FORGED" not in result.stdout
    assert json.loads(path.read_text())["session_trace"]["langfuse_secret_key"] == HOSTILE + "-new"


@pytest.mark.parametrize("body", ["{bad", "[]", '{"search": {"rrf_k": 0}}'])
def test_fragment_log_escapes_message_and_retains_exact_path(config_home, caplog, body):
    if os.name == "nt":
        pytest.skip("Windows filenames cannot contain newline or ESC; virtual-path pin covers it")
    directory = config_home / "config.d"
    directory.mkdir()
    path = directory / f"{HOSTILE}.json"
    path.write_text(body)
    config = Mem2MemConfig()
    with caplog.at_level(logging.WARNING, logger="memtomem.config"):
        cfg_mod.load_config_d(config)
    records = [r for r in caplog.records if r.name == "memtomem.config"]
    assert len(records) == 1
    record = records[0]
    assert str(directory / f"{ESCAPED}.json") in record.getMessage()
    assert record.getMessage().isprintable()
    assert record.config_fragment_path == str(path)
    assert json.loads(json.dumps({"path": record.config_fragment_path}))["path"] == str(path)
    assert path.read_text() == body
    if config.load_diagnostics:
        assert config.load_diagnostics[0].path == str(path)
    caplog.clear()
    cfg_mod.load_config_d(config, quiet=True)
    assert not caplog.records
    with pytest.raises(ConfigFragmentError) as exc:
        cfg_mod.load_config_d(config, quiet=True, strict=True)
    assert str(path) in str(exc.value)
    assert not caplog.records


def test_fragment_log_virtual_path_on_all_platforms(config_home, monkeypatch, caplog):
    path = config_home / f"{HOSTILE}\x85\ud800.json"

    # A virtual directory exercises impossible filenames on Windows too.
    class VirtualFragment:
        suffix = ".json"

        def is_file(self):
            return True

        def read_text(self, **kwargs):
            return "[]"

        def __str__(self):
            return str(path)

    directory = SimpleNamespace(is_dir=lambda: True, iterdir=lambda: [VirtualFragment()])
    monkeypatch.setattr(cfg_mod, "_config_d_path", lambda: directory)
    with caplog.at_level(logging.WARNING, logger="memtomem.config"):
        cfg_mod.load_config_d(Mem2MemConfig())
    record = next(r for r in caplog.records if r.name == "memtomem.config")
    assert (
        record.getMessage()
        == f"Config fragment {scrub_text(str(path))} is not a JSON object (ignored)"
    )
    assert record.config_fragment_path == str(path)
