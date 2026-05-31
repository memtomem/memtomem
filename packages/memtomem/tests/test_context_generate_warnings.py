"""Tests for Rules/Style generation through ``mm context generate``."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from memtomem.cli.context_cmd import context
from memtomem.context.parser import CONTEXT_FILENAME


_RULES_AND_STYLE = (
    "## Project\n\n- Name: foo\n\n"
    "## Rules\n\n- Always use type hints.\n\n"
    "## Style\n\n- Two blank lines between top-level functions.\n"
)


def _write_context(project_root: Path, body: str) -> None:
    ctx = project_root / CONTEXT_FILENAME
    ctx.parent.mkdir(parents=True, exist_ok=True)
    ctx.write_text(body, encoding="utf-8")


@pytest.fixture
def project_with_rules_and_style(tmp_path, monkeypatch):
    """Project root with .git + a context.md containing both Rules and Style."""
    (tmp_path / ".git").mkdir()
    _write_context(tmp_path, _RULES_AND_STYLE)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_generate_preserves_rules_and_style_for_cursor(project_with_rules_and_style):
    runner = CliRunner()
    result = runner.invoke(context, ["generate", "--agent=cursor"])

    assert result.exit_code == 0, result.output
    assert "Rules and Style" not in result.stderr
    assert "Rules and Style" not in result.stdout
    generated = project_with_rules_and_style.joinpath(".cursorrules").read_text(encoding="utf-8")
    assert "## Rules" in generated
    assert "## Style" in generated


def test_generate_no_warning_when_only_rules(tmp_path, monkeypatch):
    """Negative: only Rules (no Style) → no warning, no merge happens."""
    (tmp_path / ".git").mkdir()
    _write_context(
        tmp_path,
        "## Project\n\n- Name: foo\n\n## Rules\n\n- Always use type hints.\n",
    )
    monkeypatch.chdir(tmp_path)

    runner = CliRunner()
    result = runner.invoke(context, ["generate", "--agent=cursor"])

    assert result.exit_code == 0, result.output
    assert "Rules and Style" not in result.stderr


def test_generate_no_warning_for_claude_only_target(project_with_rules_and_style):
    runner = CliRunner()
    result = runner.invoke(context, ["generate", "--agent=claude"])

    assert result.exit_code == 0, result.output
    assert "Rules and Style" not in result.stderr


@pytest.mark.parametrize(
    ("agent", "path"),
    [
        ("cursor", ".cursorrules"),
        ("codex", "AGENTS.md"),
        ("copilot", ".github/copilot-instructions.md"),
    ],
)
def test_generate_preserves_rules_style_for_markdown_targets(
    project_with_rules_and_style, agent, path
):
    runner = CliRunner()
    result = runner.invoke(context, ["generate", f"--agent={agent}"])

    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    generated = project_with_rules_and_style.joinpath(path).read_text(encoding="utf-8")
    assert "## Rules" in generated
    assert "## Style" in generated
