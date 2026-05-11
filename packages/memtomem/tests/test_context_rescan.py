"""Tests for ``mm context rescan`` — privacy-only audit over generated context files.

v1 scans the set returned by ``detect_agent_files`` (CLAUDE.md, .cursorrules,
GEMINI.md, AGENTS.md, .github/copilot-instructions.md). Agents/skills/
commands runtime fanout is intentionally out of scope.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from memtomem import privacy
from memtomem.cli import cli


# Matches privacy.DEFAULT_PATTERNS (``\b(?:AKIA|ASIA)[0-9A-Z]{16}\b``).
_SECRET_LINE = "API_KEY=AKIAIOSFODNN7EXAMPLE\n"
_SAFE_LINE = "Just a benign instruction line.\n"


def _make_project_root(tmp_path: Path) -> Path:
    """Make ``tmp_path`` look like a project root so ``_find_project_root``
    returns it.

    ``_find_project_root`` (cli/context_cmd.py:105) walks up looking for
    ``.git`` or ``pyproject.toml``. A bare ``pyproject.toml`` is enough.
    """
    (tmp_path / "pyproject.toml").write_text("[project]\nname='fixture'\n")
    return tmp_path


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def _reset_privacy_counters() -> None:
    privacy.reset_for_tests()


class TestContextRescanRegistration:
    def test_rescan_help_describes_command(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["context", "rescan", "--help"])
        assert result.exit_code == 0
        assert "privacy guard" in result.output
        assert "record_outcome=False" in result.output

    def test_scope_is_required(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["context", "rescan"])
        assert result.exit_code != 0
        assert "--scope" in result.output


class TestContextRescanBehaviour:
    def test_clean_artifact_tree_exits_zero(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        root = _make_project_root(tmp_path)
        (root / "CLAUDE.md").write_text(_SAFE_LINE)
        monkeypatch.chdir(root)

        result = runner.invoke(cli, ["context", "rescan", "--scope", "project_shared"])
        assert result.exit_code == 0, result.output
        assert "0 violations" in result.output

    def test_secret_artifact_exits_one(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        root = _make_project_root(tmp_path)
        (root / "CLAUDE.md").write_text(_SECRET_LINE)
        monkeypatch.chdir(root)

        result = runner.invoke(cli, ["context", "rescan", "--scope", "project_shared"])
        assert result.exit_code == 1, result.output
        assert "CLAUDE.md" in result.output
        assert "decision=blocked" in result.output
        assert "pattern_index=" in result.output

    def test_secret_artifact_clean_inverse_passes(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Pin-and-invert: same fixture without the secret exits 0."""
        root = _make_project_root(tmp_path)
        (root / "CLAUDE.md").write_text(_SAFE_LINE)
        monkeypatch.chdir(root)

        result = runner.invoke(cli, ["context", "rescan", "--scope", "project_shared"])
        assert result.exit_code == 0, result.output

    def test_json_output_schema(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        root = _make_project_root(tmp_path)
        (root / "CLAUDE.md").write_text(_SECRET_LINE)
        monkeypatch.chdir(root)

        result = runner.invoke(cli, ["context", "rescan", "--scope", "project_shared", "--json"])
        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["scope"] == "project_shared"
        assert payload["scanned"] >= 1
        assert len(payload["violations"]) >= 1
        v = payload["violations"][0]
        assert v["path"].endswith("CLAUDE.md")
        assert v["scope"] == "project_shared"
        assert v["decision"] == "blocked"
        h = v["hits"][0]
        assert {"pattern_index", "span_start", "span_end"} <= set(h.keys())

    def test_skip_warn_reports_all_violations(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """``on_blocked='skip_warn'`` is required — fail_fast would only
        surface the first hit, leaving subsequent files unchecked. The
        full audit pins this.
        """
        root = _make_project_root(tmp_path)
        (root / "CLAUDE.md").write_text(_SECRET_LINE)
        (root / ".cursorrules").write_text(_SECRET_LINE)
        monkeypatch.chdir(root)

        result = runner.invoke(cli, ["context", "rescan", "--scope", "project_shared", "--json"])
        assert result.exit_code == 1
        payload = json.loads(result.output)
        paths = {v["path"] for v in payload["violations"]}
        assert any(p.endswith("CLAUDE.md") for p in paths)
        assert any(p.endswith(".cursorrules") for p in paths)

    def test_no_counter_drift_with_record_outcome_false(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        root = _make_project_root(tmp_path)
        (root / "CLAUDE.md").write_text(_SECRET_LINE)
        monkeypatch.chdir(root)

        before = privacy.snapshot()["outcomes"]
        result = runner.invoke(cli, ["context", "rescan", "--scope", "project_shared"])
        assert result.exit_code == 1
        after = privacy.snapshot()["outcomes"]
        for key, expected in before.items():
            assert after[key] == expected, f"counter {key!r} drifted"
