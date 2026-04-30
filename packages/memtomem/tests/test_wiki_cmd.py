"""Tests for ``mm wiki`` CLI surface (PR-A: init, list)."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from memtomem.cli.wiki_cmd import wiki


@pytest.fixture
def isolated_wiki(wiki_root: Path) -> Path:
    """Backwards-compat alias for the shared ``wiki_root`` fixture."""
    return wiki_root


class TestInitCmd:
    def test_init_creates_wiki(self, isolated_wiki: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(wiki, ["init"])
        assert result.exit_code == 0, result.output
        assert "Initialized wiki" in result.output
        assert (isolated_wiki / ".git").is_dir()
        assert (isolated_wiki / "skills").is_dir()
        assert (isolated_wiki / "agents").is_dir()
        assert (isolated_wiki / "commands").is_dir()

    def test_init_already_exists(self, isolated_wiki: Path) -> None:
        runner = CliRunner()
        runner.invoke(wiki, ["init"])
        result = runner.invoke(wiki, ["init"])
        assert result.exit_code != 0
        assert "already initialized" in result.output

    def test_init_from_url_clones(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        git_identity: None,  # noqa: ARG002
    ) -> None:
        source = tmp_path / "source"
        monkeypatch.setenv("MEMTOMEM_WIKI_PATH", str(source))

        runner = CliRunner()
        runner.invoke(wiki, ["init"])

        target = tmp_path / "target"
        monkeypatch.setenv("MEMTOMEM_WIKI_PATH", str(target))
        result = runner.invoke(wiki, ["init", "--from", f"file://{source}"])
        assert result.exit_code == 0, result.output
        assert "Cloned wiki" in result.output
        assert (target / ".git").is_dir()
        assert (target / "skills").is_dir()


class TestListCmd:
    def test_list_no_wiki(self, isolated_wiki: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(wiki, ["list"])
        assert result.exit_code != 0
        assert "wiki not found" in result.output

    def test_list_empty_wiki(self, isolated_wiki: Path) -> None:
        runner = CliRunner()
        runner.invoke(wiki, ["init"])
        result = runner.invoke(wiki, ["list"])
        assert result.exit_code == 0
        assert "no assets" in result.output

    def test_list_shows_assets(self, isolated_wiki: Path) -> None:
        runner = CliRunner()
        runner.invoke(wiki, ["init"])
        (isolated_wiki / "skills" / "code-review").mkdir()
        (isolated_wiki / "skills" / "code-review" / "SKILL.md").write_text("x", encoding="utf-8")
        (isolated_wiki / "agents" / "reviewer").mkdir()

        result = runner.invoke(wiki, ["list"])
        assert result.exit_code == 0
        assert "code-review" in result.output
        assert "reviewer" in result.output
        assert "skills/" in result.output
        assert "agents/" in result.output

    def test_list_filters_by_type(self, isolated_wiki: Path) -> None:
        runner = CliRunner()
        runner.invoke(wiki, ["init"])
        (isolated_wiki / "skills" / "alpha").mkdir()
        (isolated_wiki / "agents" / "beta").mkdir()

        result = runner.invoke(wiki, ["list", "--type", "skills"])
        assert result.exit_code == 0
        assert "alpha" in result.output
        assert "beta" not in result.output

    def test_list_rejects_unknown_type(self, isolated_wiki: Path) -> None:
        runner = CliRunner()
        runner.invoke(wiki, ["init"])
        result = runner.invoke(wiki, ["list", "--type", "widgets"])
        assert result.exit_code != 0
