"""``mm config set`` says when a change starts sending text off the machine."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from memtomem.cli import cli

from .helpers import set_home


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    set_home(monkeypatch, tmp_path)
    for name in list(os.environ):
        if name.upper().startswith("MEMTOMEM_"):
            monkeypatch.delenv(name)
    (tmp_path / ".memtomem").mkdir(parents=True)
    return tmp_path


def _write(home: Path, data: dict) -> None:
    data = {"indexing": {"auto_discover": False, **data.pop("indexing", {})}, **data}
    (home / ".memtomem" / "config.json").write_text(json.dumps(data), encoding="utf-8")


def _set(key: str, value: str) -> list[str]:
    result = CliRunner().invoke(cli, ["config", "set", key, value])
    assert result.exit_code == 0, result.output
    return [line for line in result.output.splitlines() if line.startswith("Remote inference:")]


def test_enabling_cohere_rerank_names_the_endpoint(home: Path) -> None:
    _write(home, {"rerank": {"provider": "cohere"}})
    assert _set("rerank.enabled", "true") == [
        "Remote inference: rerank provider cohere sends search queries and the text "
        "of candidate results to api.cohere.ai."
    ]


def test_enabling_local_rerank_says_nothing(home: Path) -> None:
    _write(home, {"rerank": {"provider": "fastembed"}})
    assert _set("rerank.enabled", "true") == []


def test_rerank_already_enabled_by_env_is_not_new(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(home, {"rerank": {"provider": "cohere"}})
    monkeypatch.setenv("MEMTOMEM_RERANK__ENABLED", "true")
    assert _set("rerank.enabled", "true") == []


def test_shadowed_enable_says_nothing(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Saved but outranked by the environment: nothing new is in effect."""
    _write(home, {"rerank": {"provider": "cohere"}})
    monkeypatch.setenv("MEMTOMEM_RERANK__ENABLED", "false")
    result = CliRunner().invoke(cli, ["config", "set", "rerank.enabled", "true"])
    assert result.exit_code == 0, result.output
    assert "Remote inference:" not in result.output
    assert "MEMTOMEM_RERANK__ENABLED" in result.output


def test_auto_summarize_with_remote_llm_names_the_llm(home: Path) -> None:
    _write(home, {"llm": {"enabled": True, "provider": "anthropic"}})
    assert _set("indexing.auto_summarize", "true") == [
        "Remote inference: indexing.auto_summarize sends note text to llm provider "
        "anthropic at api.anthropic.com."
    ]


@pytest.mark.parametrize(
    "llm",
    [
        {"enabled": False, "provider": "anthropic"},
        {"enabled": True, "provider": "ollama"},
    ],
)
def test_auto_summarize_without_remote_llm_says_nothing(home: Path, llm: dict) -> None:
    _write(home, {"llm": llm})
    assert _set("indexing.auto_summarize", "true") == []


def test_disabling_says_nothing(home: Path) -> None:
    _write(
        home,
        {
            "rerank": {"provider": "cohere", "enabled": True},
            "llm": {"enabled": True, "provider": "anthropic"},
            "indexing": {"auto_summarize": True},
        },
    )
    assert _set("rerank.enabled", "false") == []
    assert _set("indexing.auto_summarize", "false") == []


def test_unrelated_key_says_nothing_even_with_remote_config(home: Path) -> None:
    _write(home, {"embedding": {"provider": "openai", "model": "text-embedding-3-small"}})
    assert _set("search.default_top_k", "12") == []
