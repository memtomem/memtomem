"""New namespace config values must be findable by name; old ones still load (#2946).

``namespace.default_namespace`` and the ``namespace`` template of each
``namespace.rules`` entry go through ``validate_writable_namespace`` when a
surface writes them — ``mm config set``, the web ``PATCH /api/config``,
``mem_config`` and ``mm init``. The check is a ``write_validator``, which the
``config.json`` loader skips and the fragment loader never reads, so a config
saved before the check existed keeps loading with its value intact; the
indexer then skips such a name at resolution time.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from click.testing import CliRunner
from httpx import ASGITransport, AsyncClient

from memtomem import config as cfg_mod
from memtomem.cli import cli
from memtomem.config import (
    FIELD_CONSTRAINTS,
    Mem2MemConfig,
    NamespacePolicyRule,
    coerce_and_validate,
)
from memtomem.server.tools import status_config

from .helpers import isolate_config_paths, set_home

REFUSED_DEFAULTS = ["x*y", "a,b", "!system", "  ", "agent-runtime:foo:bar"]
ACCEPTED_DEFAULTS = ["default", "team", "claude:-Users-x", "agent-runtime:planner"]
REFUSED_TEMPLATES = ["a,b", "{parent},x", "!system", "x*{parent}", "agent-runtime:{parent}:y"]
ACCEPTED_TEMPLATES = ["{parent}", "claude:{parent}", "agent-runtime:{parent}", "docs"]

DEFAULT = FIELD_CONSTRAINTS["namespace.default_namespace"]
RULES = FIELD_CONSTRAINTS["namespace.rules"]


def _rule(namespace: str) -> dict:
    return {"path_glob": "docs/**/*.md", "namespace": namespace}


@pytest.fixture
def config_home(tmp_path, monkeypatch):
    set_home(monkeypatch, tmp_path)
    return isolate_config_paths(monkeypatch, tmp_path / ".memtomem")


class TestConstraint:
    @pytest.mark.parametrize("value", REFUSED_DEFAULTS)
    def test_default_refused_on_write(self, value):
        with pytest.raises(ValueError, match="invalid namespace"):
            coerce_and_validate(value, DEFAULT)

    @pytest.mark.parametrize("value", REFUSED_DEFAULTS)
    def test_default_accepted_on_load(self, value):
        assert coerce_and_validate(value, DEFAULT, for_write=False) == value

    @pytest.mark.parametrize("value", ACCEPTED_DEFAULTS)
    def test_default_accepted(self, value):
        assert coerce_and_validate(value, DEFAULT) == value

    @pytest.mark.parametrize("template", REFUSED_TEMPLATES)
    def test_rule_template_refused_on_write(self, template):
        with pytest.raises(ValueError, match="item\\[1\\] namespace"):
            coerce_and_validate(json.dumps([_rule("docs"), _rule(template)]), RULES)

    @pytest.mark.parametrize("template", REFUSED_TEMPLATES)
    def test_rule_template_accepted_on_load(self, template):
        rules = coerce_and_validate([_rule(template)], RULES, for_write=False)
        assert [r.namespace for r in rules] == [template]

    @pytest.mark.parametrize("template", ACCEPTED_TEMPLATES)
    def test_rule_template_accepted(self, template):
        rules = coerce_and_validate([_rule(template)], RULES)
        assert [r.namespace for r in rules] == [template]


class TestWriteSurfaces:
    def test_mm_config_set_refuses_without_persisting(self, config_home):
        path = config_home / "config.json"
        path.write_text(json.dumps({"indexing": {"auto_discover": False}}))
        before = path.read_bytes()

        for key, value in (
            ("namespace.default_namespace", "x*y"),
            ("namespace.rules", json.dumps([_rule("a,b")])),
        ):
            result = CliRunner().invoke(cli, ["config", "set", key, value])
            assert result.exit_code != 0, result.output
            assert "invalid namespace" in result.output
        assert path.read_bytes() == before

    def test_mm_config_set_accepts_a_valid_default(self, config_home):
        path = config_home / "config.json"
        path.write_text(json.dumps({"indexing": {"auto_discover": False}}))
        result = CliRunner().invoke(cli, ["config", "set", "namespace.default_namespace", "team"])
        assert result.exit_code == 0, result.output
        assert json.loads(path.read_text())["namespace"]["default_namespace"] == "team"

    @pytest.mark.parametrize("persist", [False, True])
    async def test_mem_config_refuses_before_mutation(self, monkeypatch, persist):
        config = Mem2MemConfig()
        app = SimpleNamespace(
            config=config,
            search_pipeline=SimpleNamespace(invalidate_cache=MagicMock()),
        )
        monkeypatch.setattr(status_config, "_get_app_initialized", AsyncMock(return_value=app))
        save = MagicMock()
        monkeypatch.setattr(cfg_mod, "save_config_overrides", save)

        response = await status_config.mem_config(
            "namespace.default_namespace", "!system", persist=persist
        )

        assert response.startswith("Invalid value")
        assert "invalid namespace" in response
        assert config.namespace.default_namespace == "default"
        save.assert_not_called()

    async def test_web_patch_rejects_without_persisting(self, tmp_path, monkeypatch):
        from memtomem.web import hot_reload
        from memtomem.web.app import create_app

        set_home(monkeypatch, tmp_path)
        for k in list(os.environ):
            if k.startswith("MEMTOMEM_"):
                monkeypatch.delenv(k, raising=False)
        cfg = tmp_path / ".memtomem" / "config.json"
        cfg.parent.mkdir(parents=True)
        cfg.write_text(json.dumps({"indexing": {"auto_discover": False}}), encoding="utf-8")
        before = cfg.read_bytes()

        app = create_app(lifespan=None, mode="dev")
        app.state.csrf_enforce = False
        app.state.storage = AsyncMock()
        app.state.search_pipeline = AsyncMock()
        app.state.search_pipeline.invalidate_cache = MagicMock()
        app.state.index_engine = AsyncMock()
        app.state.embedder = AsyncMock()
        app.state.dedup_scanner = AsyncMock()
        app.state.config = hot_reload._build_fresh_config()
        app.state.config_signature = hot_reload.current_signature()
        app.state.last_reload_error = None

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.patch(
                "/api/config",
                params={"persist": "true"},
                json={"namespace": {"default_namespace": "x*y", "rules": [_rule("a,b")]}},
            )

        body = resp.json()
        rejected = " ".join(body.get("rejected", []))
        assert "namespace.default_namespace" in rejected, body
        assert "namespace.rules" in rejected, body
        assert app.state.config.namespace.default_namespace == "default"
        assert cfg.read_bytes() == before


class TestLoadKeepsLegacyValues:
    def test_config_json_loads_a_refused_default_and_rule(self, config_home, caplog):
        path = config_home / "config.json"
        path.write_text(
            json.dumps({"namespace": {"default_namespace": "x*y", "rules": [_rule("a,b")]}})
        )
        config = Mem2MemConfig()
        with caplog.at_level(logging.WARNING):
            cfg_mod.load_config_overrides(config, migrate=False)
        assert config.namespace.default_namespace == "x*y"
        assert [r.namespace for r in config.namespace.rules] == ["a,b"]
        assert "namespace." not in caplog.text

    def test_strict_fragment_loads_a_refused_default_and_rule(self, config_home):
        directory = config_home / "config.d"
        directory.mkdir()
        (directory / "10-ns.json").write_text(
            json.dumps({"namespace": {"default_namespace": "x*y", "rules": [_rule("a,b")]}})
        )
        config = Mem2MemConfig()
        # ``strict`` raises ConfigFragmentError on any skipped value; the
        # server's reload path uses it.
        cfg_mod.load_config_d(config, strict=True)
        assert config.namespace.default_namespace == "x*y"
        assert [r.namespace for r in config.namespace.rules] == [
            NamespacePolicyRule(**_rule("a,b")).namespace
        ]


class TestInit:
    def test_namespace_flag_refused_before_anything_is_written(self, tmp_path, monkeypatch):
        set_home(monkeypatch, tmp_path)
        mem_dir = tmp_path / "mem"
        result = CliRunner().invoke(
            cli,
            [
                "init",
                "--non-interactive",
                "--provider",
                "none",
                "--memory-dir",
                str(mem_dir),
                "--namespace",
                "x*y",
                "--mcp",
                "skip",
            ],
        )
        assert result.exit_code == 2, result.output
        assert "--namespace" in result.output and "invalid namespace" in result.output
        assert not (tmp_path / ".memtomem" / "config.json").exists()
        assert not mem_dir.exists()

    def test_wizard_asks_again_for_a_refused_default(self, monkeypatch):
        from memtomem.cli import init_cmd

        answers = iter(["x*y", "!system", "team"])
        monkeypatch.setattr(init_cmd, "nav_prompt", lambda *_a, **_k: next(answers))
        monkeypatch.setattr(init_cmd, "nav_confirm", lambda *_a, **_k: False)
        monkeypatch.setattr(init_cmd, "step_header", lambda *_a, **_k: None)
        state: dict = {}

        init_cmd._step_namespace(state)

        assert state["default_ns"] == "team"
        assert next(answers, None) is None


def test_load_caller_is_the_only_for_write_false():
    """A new loader must opt out explicitly; every other caller writes."""
    src = Path(cfg_mod.__file__).read_text(encoding="utf-8")
    assert src.count("for_write=False") == 1
