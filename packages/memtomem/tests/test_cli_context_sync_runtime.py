"""ADR-0030 PR-C — ``mm context sync --runtime`` additive fan-out filter.

The engine already accepts a ``runtimes`` subset; these pin the CLI plumbing:
the bare-name → generator-name mapping, the settings/mcp-servers/no-artifact
UsageError boundary, and that omitting the flag is byte-identical (all
runtimes).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from memtomem.cli.context_cmd import context
from memtomem.context.scope_resolver import canonical_artifact_dir


@pytest.fixture
def proj(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    p = tmp_path / "proj"
    (p / ".git").mkdir(parents=True)
    (p / ".memtomem").mkdir()
    # A canonical skill to fan out + a context.md so the memory leg is happy.
    d = canonical_artifact_dir("skills", "project_shared", p) / "myskill"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: myskill\n---\nbody\n", encoding="utf-8")
    (p / ".memtomem" / "context.md").write_text("## Overview\nhi\n", encoding="utf-8")
    monkeypatch.chdir(p)
    return p


def _invoke(args: list[str], **kw: object):
    return CliRunner().invoke(context, args, **kw)  # type: ignore[arg-type]


def _has_skill(proj: Path, runtime: str) -> bool:
    return (proj / f".{runtime}" / "skills" / "myskill").exists()


def test_runtime_filter_fans_out_only_selected(proj: Path) -> None:
    res = _invoke(
        ["sync", "--runtime", "claude", "--include", "skills", "--scope", "project_shared"]
    )
    assert res.exit_code == 0, res.output
    assert _has_skill(proj, "claude")
    assert not _has_skill(proj, "gemini")


def test_no_filter_fans_out_all(proj: Path) -> None:
    res = _invoke(["sync", "--include", "skills", "--scope", "project_shared"])
    assert res.exit_code == 0, res.output
    assert _has_skill(proj, "claude")
    assert _has_skill(proj, "gemini")


def test_repeatable_runtime_union(proj: Path) -> None:
    res = _invoke(
        [
            "sync",
            "--runtime",
            "claude",
            "--runtime",
            "gemini",
            "--include",
            "skills",
            "--scope",
            "project_shared",
        ]
    )
    assert res.exit_code == 0, res.output
    assert _has_skill(proj, "claude")
    assert _has_skill(proj, "gemini")


def test_unknown_runtime_combo_is_visible_skip_not_error(proj: Path) -> None:
    """--runtime codex --include commands has no codex-commands generator → a
    visible unknown-runtime skip, exit 0 (not a silent no-op, not an error)."""
    d = canonical_artifact_dir("commands", "project_shared", proj) / "c"
    d.mkdir(parents=True)
    (d / "command.md").write_text("# c\nbody\n", encoding="utf-8")
    res = _invoke(
        ["sync", "--runtime", "codex", "--include", "commands", "--scope", "project_shared"]
    )
    assert res.exit_code == 0, res.output
    assert "unknown runtime" in res.output.lower() or "codex_commands" in res.output


@pytest.mark.parametrize("bad", [["--include", "settings"], ["--include", "mcp-servers"], []])
def test_runtime_without_artifact_kind_usage_error(proj: Path, bad: list[str]) -> None:
    res = _invoke(["sync", "--runtime", "claude", *bad])
    assert res.exit_code == 2
