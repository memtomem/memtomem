"""Namespace globs must compile before configuration accepts them (#2432)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from click.testing import CliRunner
from pydantic import ValidationError

from memtomem import config as cfg_mod
from memtomem.cli import cli
from memtomem.config import Mem2MemConfig, NamespacePolicyRule
from memtomem.indexing.engine import _build_exclude_spec
from memtomem.server.tools import status_config

from .helpers import isolate_config_paths, set_home


INVALID_GLOBS = ["!", "foo\\", "[z-a]", "[Z-a]"]


@pytest.fixture
def config_home(tmp_path, monkeypatch):
    set_home(monkeypatch, tmp_path)
    return isolate_config_paths(monkeypatch, tmp_path / ".memtomem")


def _rule(pattern="docs/**/*.md"):
    return {"path_glob": pattern, "namespace": "docs"}


@pytest.mark.parametrize("pattern", INVALID_GLOBS)
def test_invalid_glob_is_a_field_validation_error(pattern):
    with pytest.raises(ValidationError) as caught:
        NamespacePolicyRule(**_rule(pattern))

    error = caught.value.errors()[0]
    assert error["loc"] == ("path_glob",)
    assert error["type"] == "value_error"
    # Retain the actual compiler diagnostic, including regex errors that are
    # not ValueError subclasses and would otherwise escape Pydantic.
    with pytest.raises(Exception) as compiler_error:
        _build_exclude_spec([pattern])
    assert str(compiler_error.value) in error["msg"]


@pytest.mark.parametrize("pattern", ["***", "[", "a/[b", "Docs/**/*.MD", "[a-z]/**"])
def test_accepted_globs_still_compile_with_the_engine(pattern):
    rule = NamespacePolicyRule(**_rule(f"  {pattern}  "))
    assert rule.path_glob == pattern
    _build_exclude_spec([rule.path_glob])


@pytest.mark.parametrize("home_form", [True, False], ids=["tilde", "absolute"])
def test_normalized_paths_keep_case_and_match(config_home, home_form):
    target = Path.home() / "Docs" / "note.md"
    pattern = "~/Docs/**" if home_form else str(target.parent / "**")
    rule = NamespacePolicyRule(**_rule(pattern))
    assert rule.path_glob == (target.parent / "**").as_posix()
    spec = _build_exclude_spec([rule.path_glob])
    assert spec.match_file(target.as_posix().lstrip("/").lower())


@pytest.mark.parametrize("pattern", INVALID_GLOBS)
def test_config_json_rejects_invalid_rules_and_keeps_prior_rules(config_home, caplog, pattern):
    path = config_home / "config.json"
    path.write_text(json.dumps({"namespace": {"rules": [_rule(), _rule(pattern)]}}))
    before = path.read_bytes()
    config = Mem2MemConfig()
    prior = [NamespacePolicyRule(path_glob="prior/**", namespace="prior")]
    config.namespace.rules = prior

    cfg_mod.load_config_overrides(config, migrate=False)

    assert config.namespace.rules == prior
    assert "namespace.rules" in caplog.text
    assert "path_glob" in caplog.text
    assert path.read_bytes() == before
    for rule in config.namespace.rules:
        _build_exclude_spec([rule.path_glob])


@pytest.mark.parametrize("pattern", INVALID_GLOBS)
def test_config_d_skips_invalid_rule_and_keeps_valid_rules(config_home, caplog, pattern):
    directory = config_home / "config.d"
    directory.mkdir()
    path = directory / "10-rules.json"
    path.write_text(json.dumps({"namespace": {"rules": [_rule(pattern), _rule()]}}))
    before = path.read_bytes()
    config = Mem2MemConfig()

    cfg_mod.load_config_d(config)

    assert config.namespace.rules == [NamespacePolicyRule(**_rule())]
    assert "namespace.rules" in caplog.text
    assert "path_glob" in caplog.text
    assert path.read_bytes() == before


@pytest.mark.parametrize("pattern", INVALID_GLOBS)
def test_cli_rejects_invalid_glob_without_persisting(config_home, pattern):
    path = config_home / "config.json"
    path.write_text(
        json.dumps({"namespace": {"rules": [_rule()]}, "indexing": {"auto_discover": False}})
    )
    before = path.read_bytes()

    result = CliRunner().invoke(
        cli, ["config", "set", "namespace.rules", json.dumps([_rule(pattern)])]
    )

    assert result.exit_code != 0
    assert "path_glob" in result.output
    assert path.read_bytes() == before


@pytest.mark.parametrize("pattern", INVALID_GLOBS)
@pytest.mark.parametrize("persist", [False, True])
async def test_mcp_rejects_invalid_glob_before_mutation(config_home, monkeypatch, pattern, persist):
    config = Mem2MemConfig()
    prior = [NamespacePolicyRule(**_rule())]
    config.namespace.rules = prior
    app = SimpleNamespace(
        config=config,
        search_pipeline=SimpleNamespace(invalidate_cache=MagicMock()),
    )
    monkeypatch.setattr(status_config, "_get_app_initialized", AsyncMock(return_value=app))
    save = MagicMock()
    monkeypatch.setattr(cfg_mod, "save_config_overrides", save)

    response = await status_config.mem_config(
        "namespace.rules", json.dumps([_rule(pattern)]), persist=persist
    )

    assert response.startswith("Invalid value")
    assert "path_glob" in response
    assert config.namespace.rules is prior
    save.assert_not_called()
    app.search_pipeline.invalidate_cache.assert_not_called()
