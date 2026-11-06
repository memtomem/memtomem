"""``mm init`` lists the remote inference endpoints the written config puts in effect."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from memtomem.cli.init_cmd import init

from .helpers import set_home

HEADER = "Remote inference endpoints — note text leaves this machine:"


@pytest.fixture(autouse=True)
def _clean_claude_inventory(monkeypatch):
    from memtomem.cli import _claude_mcp

    monkeypatch.setattr(_claude_mcp, "inspect_claude_mcp", lambda: _claude_mcp.Report())


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    set_home(monkeypatch, tmp_path)
    for name in list(os.environ):
        if name.upper().startswith("MEMTOMEM_"):
            monkeypatch.delenv(name)
    return tmp_path


def _init(home: Path, *args: str):
    result = CliRunner().invoke(
        init,
        ["--non-interactive", "--memory-dir", str(home / "mem"), "--mcp", "skip", *args],
    )
    assert result.exit_code == 0, result.output
    return result


def _listed(text: str) -> list[str]:
    lines = text.splitlines()
    if not any(HEADER in line for line in lines):
        return []
    start = next(i for i, line in enumerate(lines) if HEADER in line)
    return [line.strip() for line in lines[start + 1 :] if line.startswith("    - ")]


def test_openai_provider_is_listed_on_stderr(home: Path) -> None:
    result = _init(home, "--provider", "openai", "--api-key", "sk-test")
    assert _listed(result.stderr) == [
        "- embedding (openai) -> api.openai.com: sends note text and search queries"
    ]
    assert HEADER not in result.stdout
    assert "Not covered: Langfuse session tracing" in result.stderr


def test_local_setup_lists_nothing(home: Path) -> None:
    result = _init(home, "--provider", "none")
    assert HEADER not in result.output


def test_preserved_remote_llm_is_listed(home: Path) -> None:
    """A remote LLM kept from an earlier config, which no wizard step shows."""
    config_dir = home / ".memtomem"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(
        json.dumps({"llm": {"enabled": True, "provider": "anthropic"}}), encoding="utf-8"
    )
    result = _init(home, "--provider", "none")
    assert _listed(result.stderr) == [
        "- llm (anthropic) -> api.anthropic.com: sends note text, queries and session "
        "notes for the enabled LLM features"
    ]


def test_env_override_is_listed(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMTOMEM_RERANK__ENABLED", "true")
    monkeypatch.setenv("MEMTOMEM_RERANK__PROVIDER", "cohere")
    result = _init(home, "--provider", "none")
    assert _listed(result.stderr) == [
        "- rerank (cohere) -> api.cohere.ai: sends search queries and the text of candidate results"
    ]


def test_listed_before_the_seed_offer(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from memtomem.cli import init_cmd

    order: list[str] = []
    real = init_cmd._emit_remote_inference_summary

    def spy_summary(*args, **kwargs):
        order.append("summary")
        return real(*args, **kwargs)

    def spy_seed(paths, state):
        order.append("seed")
        return False

    monkeypatch.setattr(init_cmd, "_emit_remote_inference_summary", spy_summary)
    monkeypatch.setattr(init_cmd, "_maybe_seed_initial_index", spy_seed)
    _init(home, "--provider", "openai", "--api-key", "sk-test")
    assert order == ["summary", "seed"]
