"""Tests for the Rules+Style merge stderr warning in ``mm context generate``.

Cursor / Codex / Copilot generators concatenate the ``Rules`` and ``Style``
context.md sections under a single heading via ``_compact_rules`` (see
:mod:`memtomem.context.generator`). When both sections are populated and at
least one merging runtime is targeted, the CLI should emit a single stderr
warning naming only the affected runtimes — generated file format itself
is unchanged.

Click 8.3 keeps ``result.stderr`` separate from ``result.output`` (stdout) by
default; assertions below pin both surfaces.
"""

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


def test_generate_warns_on_rules_style_merge_for_cursor(project_with_rules_and_style):
    """Positive: --agent=cursor with both sections → warning on stderr."""
    runner = CliRunner()
    result = runner.invoke(context, ["generate", "--agent=cursor"])

    assert result.exit_code == 0, result.output
    assert "Rules and Style" in result.stderr
    assert "cursor" in result.stderr
    # Stdout must not contain the warning (Click 8.3 separates streams;
    # ``result.output`` is interleaved, ``result.stdout`` is stdout-only).
    assert "Rules and Style" not in result.stdout


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
    """Negative: --agent=claude does not use ``_compact_rules`` → no warning."""
    runner = CliRunner()
    result = runner.invoke(context, ["generate", "--agent=claude"])

    assert result.exit_code == 0, result.output
    assert "Rules and Style" not in result.stderr


def test_generate_warning_lists_only_intersecting_runtimes(project_with_rules_and_style):
    """The warning names only runtimes that intersect with the target set.

    With ``--agent=cursor``, codex and copilot must NOT appear — otherwise
    the message implies the user is generating files they did not request.
    """
    runner = CliRunner()
    result = runner.invoke(context, ["generate", "--agent=cursor"])

    assert result.exit_code == 0, result.output
    assert "cursor" in result.stderr
    assert "codex" not in result.stderr
    assert "copilot" not in result.stderr
