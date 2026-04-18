"""Tests for mm purge --matching-excluded."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from click.testing import CliRunner

from memtomem.cli import cli
from memtomem.cli.purge_cmd import find_sources_matching_excluded


def _mock_components(matched_sources, indexing_exclude_patterns=()):
    """Build a fake Components-like object where storage returns the given sources."""
    storage = SimpleNamespace(
        get_all_source_files=AsyncMock(return_value=set(matched_sources)),
        list_chunks_by_sources=AsyncMock(
            return_value={sf: [object(), object()] for sf in matched_sources}
        ),
        delete_by_source=AsyncMock(return_value=2),
    )
    config = SimpleNamespace(
        indexing=SimpleNamespace(exclude_patterns=list(indexing_exclude_patterns))
    )
    return SimpleNamespace(storage=storage, config=config)


def _patched_cli_components(comp):
    @asynccontextmanager
    async def fake():
        yield comp

    return fake


class TestFindSourcesMatchingExcluded:
    """Pure-function core used by both the CLI and tests."""

    def test_builtin_secret_matches(self):
        sources = {
            Path("/home/u/.gemini/oauth_creds.json"),
            Path("/home/u/notes/day.md"),
        }
        matched = find_sources_matching_excluded(sources, user_patterns=[])
        assert Path("/home/u/.gemini/oauth_creds.json") in matched
        assert Path("/home/u/notes/day.md") not in matched

    def test_builtin_noise_matches(self):
        sources = {
            Path("/home/u/.claude/projects/abc/subagents/x.meta.json"),
            Path("/home/u/notes/day.md"),
        }
        matched = find_sources_matching_excluded(sources, user_patterns=[])
        assert Path("/home/u/.claude/projects/abc/subagents/x.meta.json") in matched

    def test_user_pattern_matches(self):
        sources = {Path("/home/u/drafts/todo.md"), Path("/home/u/notes/day.md")}
        matched = find_sources_matching_excluded(sources, user_patterns=["**/drafts/**"])
        assert Path("/home/u/drafts/todo.md") in matched
        assert Path("/home/u/notes/day.md") not in matched

    def test_user_negation_cannot_unset_builtin(self):
        """Security regression: user cannot whitelist a built-in secret."""
        sources = {Path("/home/u/.gemini/oauth_creds.json")}
        matched = find_sources_matching_excluded(sources, user_patterns=["!**/oauth_creds.json"])
        assert Path("/home/u/.gemini/oauth_creds.json") in matched

    def test_case_insensitive(self):
        sources = {Path("/home/u/OAuth_Creds.JSON")}
        matched = find_sources_matching_excluded(sources, user_patterns=[])
        assert Path("/home/u/OAuth_Creds.JSON") in matched


class TestPurgeCLI:
    def test_help(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["purge", "--help"])
        assert result.exit_code == 0
        assert "--matching-excluded" in result.output
        assert "--apply" in result.output

    def test_no_selector_errors(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["purge"])
        assert result.exit_code != 0
        assert "selector" in result.output.lower()

    def test_dry_run_does_not_delete(self, monkeypatch):
        """Dry-run reports counts and sample; never calls delete_by_source."""
        comp = _mock_components([Path("/home/u/.gemini/oauth_creds.json")])
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))

        result = CliRunner().invoke(cli, ["purge", "--matching-excluded"])

        assert result.exit_code == 0, result.output
        assert "Would delete" in result.output
        assert "oauth_creds.json" in result.output
        assert "--apply" in result.output
        comp.storage.delete_by_source.assert_not_called()

    def test_apply_deletes(self, monkeypatch):
        """--apply calls delete_by_source per matched file."""
        sources = [
            Path("/home/u/.gemini/oauth_creds.json"),
            Path("/home/u/.ssh/id_rsa.pub"),
        ]
        comp = _mock_components(sources)
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))

        result = CliRunner().invoke(cli, ["purge", "--matching-excluded", "--apply"])

        assert result.exit_code == 0, result.output
        assert "Deleted" in result.output
        assert comp.storage.delete_by_source.call_count == len(sources)

    def test_no_matches_reports_clean(self, monkeypatch):
        """When no stored source matches, report and return without touching delete."""
        comp = _mock_components([Path("/home/u/notes/day.md")])
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))

        result = CliRunner().invoke(cli, ["purge", "--matching-excluded"])

        assert result.exit_code == 0, result.output
        assert "No stored chunks match" in result.output
        comp.storage.delete_by_source.assert_not_called()
