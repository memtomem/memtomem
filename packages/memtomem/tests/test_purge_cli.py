"""Tests for mm purge --matching-excluded."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from click.testing import CliRunner

from memtomem.cli import cli
from memtomem.cli.purge_cmd import find_sources_matching_excluded


def _mock_components(matched_sources, indexing_exclude_patterns=(), memory_dirs=()):
    """Build a fake Components-like object where storage returns the given sources."""
    storage = SimpleNamespace(
        get_all_source_files=AsyncMock(return_value=set(matched_sources)),
        count_chunks_by_sources=AsyncMock(return_value={sf: 2 for sf in matched_sources}),
        delete_by_source=AsyncMock(return_value=2),
    )
    roots = list(memory_dirs)
    config = SimpleNamespace(
        indexing=SimpleNamespace(
            exclude_patterns=list(indexing_exclude_patterns),
            all_index_roots=lambda: roots,
        )
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
        matched = find_sources_matching_excluded(sources, user_patterns=[], memory_dirs=[])
        assert Path("/home/u/.gemini/oauth_creds.json") in matched
        assert Path("/home/u/notes/day.md") not in matched

    def test_builtin_noise_matches(self):
        sources = {
            Path("/home/u/.claude/projects/abc/subagents/x.meta.json"),
            Path("/home/u/notes/day.md"),
        }
        matched = find_sources_matching_excluded(sources, user_patterns=[], memory_dirs=[])
        assert Path("/home/u/.claude/projects/abc/subagents/x.meta.json") in matched

    def test_user_pattern_matches(self):
        sources = {Path("/home/u/drafts/todo.md"), Path("/home/u/notes/day.md")}
        matched = find_sources_matching_excluded(
            sources, user_patterns=["**/drafts/**"], memory_dirs=[]
        )
        assert Path("/home/u/drafts/todo.md") in matched
        assert Path("/home/u/notes/day.md") not in matched

    def test_user_negation_cannot_unset_builtin(self):
        """Security regression: user cannot whitelist a built-in secret."""
        sources = {Path("/home/u/.gemini/oauth_creds.json")}
        matched = find_sources_matching_excluded(
            sources, user_patterns=["!**/oauth_creds.json"], memory_dirs=[]
        )
        assert Path("/home/u/.gemini/oauth_creds.json") in matched

    def test_case_insensitive(self):
        sources = {Path("/home/u/OAuth_Creds.JSON")}
        matched = find_sources_matching_excluded(sources, user_patterns=[], memory_dirs=[])
        assert Path("/home/u/OAuth_Creds.JSON") in matched

    def test_provider_index_convention_matches(self):
        """A claude-memory root's MEMORY.md/README.md are index/meta, not content."""
        root = Path("/home/u/.claude/projects/slug/memory")
        sources = {root / "MEMORY.md", root / "README.md", root / "feedback_note.md"}
        matched = find_sources_matching_excluded(sources, user_patterns=[], memory_dirs=[root])
        assert root / "MEMORY.md" in matched
        assert root / "README.md" in matched
        assert root / "feedback_note.md" not in matched

    def test_memory_md_outside_provider_root_not_excluded(self):
        """MEMORY.md in a plain user dir is real content — the convention is provider-scoped."""
        root = Path("/home/u/notes")
        sources = {root / "MEMORY.md"}
        matched = find_sources_matching_excluded(sources, user_patterns=[], memory_dirs=[root])
        assert root / "MEMORY.md" not in matched


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


class TestPurgeJson:
    """``--json`` write acks (#1615) — CONTRIBUTING write-command shape."""

    def test_dry_run_json_ack(self, monkeypatch):
        comp = _mock_components([Path("/home/u/.gemini/oauth_creds.json")])
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))

        result = CliRunner().invoke(cli, ["purge", "--matching-excluded", "--json"])

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data == {
            "ok": True,
            "apply": False,
            "files": 1,
            "chunks": 2,
            # str(Path(...)) keeps the assertion portable across separators.
            "sample": [str(Path("/home/u/.gemini/oauth_creds.json"))],
        }
        comp.storage.delete_by_source.assert_not_called()

    def test_apply_json_ack(self, monkeypatch):
        sources = [
            Path("/home/u/.gemini/oauth_creds.json"),
            Path("/home/u/.ssh/id_rsa.pub"),
        ]
        comp = _mock_components(sources)
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))

        result = CliRunner().invoke(cli, ["purge", "--matching-excluded", "--apply", "--json"])

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        # delete_by_source is mocked to return 2 per file.
        assert data == {"ok": True, "apply": True, "files": 2, "chunks": 4}
        # Apply reports what it deleted, so it must not pay for a preview count
        # it never prints — the reason the count moved inside the dry-run
        # branch rather than staying above it (#2261).
        comp.storage.count_chunks_by_sources.assert_not_called()

    def test_no_matches_json_is_ok_noop(self, monkeypatch):
        comp = _mock_components([Path("/home/u/notes/day.md")])
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))

        result = CliRunner().invoke(cli, ["purge", "--matching-excluded", "--json"])

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data == {"ok": True, "apply": False, "files": 0, "chunks": 0, "sample": []}
        comp.storage.delete_by_source.assert_not_called()


class TestIncompleteScan:
    """A source the exclusion predicate raises on makes the scan incomplete (#2486).

    The predicate raises rather than answering "not excluded" (that bypassed the
    secret denylist), so purge must neither report a clean result nor delete on
    a partial selection.
    """

    SECRET = Path("/home/u/.gemini/oauth_creds.json")
    PLAIN = Path("/home/u/notes/day.md")
    BAD = Path("/home/u/notes/unclassifiable.md")

    @staticmethod
    def _components(sources):
        comp = _mock_components(sources)
        # Count only what was asked for: the shared mock answers for every source,
        # which would hide a count taken over the unclassified rows too.
        comp.storage.count_chunks_by_sources = AsyncMock(
            side_effect=lambda asked: {sf: 2 for sf in asked}
        )
        return comp

    @staticmethod
    def _raise_for(monkeypatch, error, predicate=lambda sf: "unclassifiable" in str(sf)):
        from memtomem.indexing import engine

        real = engine._path_is_excluded

        def fake(file_path, *args, **kwargs):
            if predicate(file_path):
                raise error("cannot classify")
            return real(file_path, *args, **kwargs)

        monkeypatch.setattr(engine, "_path_is_excluded", fake)

    @pytest.mark.parametrize("error", [OSError, ValueError, RuntimeError])
    @pytest.mark.parametrize("order", ["bad_first", "bad_last"])
    def test_apply_refuses_and_reports_matches_and_unclassified(self, monkeypatch, error, order):
        sources = [self.SECRET, self.PLAIN, self.BAD]
        if order == "bad_first":
            sources.reverse()
        comp = self._components(sources)
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))
        self._raise_for(monkeypatch, error)

        result = CliRunner().invoke(cli, ["purge", "--matching-excluded", "--apply", "--json"])

        assert result.exit_code == 1, result.output
        assert json.loads(result.stdout) == {
            "ok": False,
            "reason": "unclassifiable_sources",
            "apply": True,
            "files": 1,
            "chunks": 2,
            "sample": [str(self.SECRET)],
            "deleted_chunks": 0,
            "unclassified": 1,
            "unclassified_sample": [str(self.BAD)],
        }
        comp.storage.delete_by_source.assert_not_called()

    @pytest.mark.parametrize("apply_flag", [[], ["--apply"]])
    def test_every_source_unclassifiable_is_not_a_clean_no_match(self, monkeypatch, apply_flag):
        """A looping configured root fails the ownership lookup for every source."""
        sources = [self.SECRET, self.PLAIN, self.BAD]
        comp = self._components(sources)
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))
        self._raise_for(monkeypatch, RuntimeError, predicate=lambda sf: True)

        result = CliRunner().invoke(cli, ["purge", "--matching-excluded", "--json", *apply_flag])

        assert result.exit_code == 1, result.output
        data = json.loads(result.stdout)
        assert data["ok"] is False
        assert data["files"] == 0
        assert data["deleted_chunks"] == 0
        assert data["unclassified"] == len(sources)
        assert data["unclassified_sample"] == sorted(str(s) for s in sources)
        comp.storage.delete_by_source.assert_not_called()

    def test_text_output_warns_on_stderr_and_keeps_the_match_summary(self, monkeypatch):
        comp = self._components([self.SECRET, self.BAD])
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))
        self._raise_for(monkeypatch, ValueError)

        result = CliRunner().invoke(cli, ["purge", "--matching-excluded"])

        assert result.exit_code == 1, result.output
        assert "Could not classify 1 stored source(s); the scan is incomplete." in result.stderr
        assert str(self.BAD) in result.stderr
        assert "Classified matches: 2 chunks across 1 files" in result.stdout
        assert "No stored chunks match" not in result.output
        comp.storage.delete_by_source.assert_not_called()

    def test_find_sources_matching_excluded_raises_rather_than_omitting(self, monkeypatch):
        from memtomem.cli.purge_cmd import UnclassifiableSourcesError

        self._raise_for(monkeypatch, OSError)

        with pytest.raises(UnclassifiableSourcesError) as caught:
            find_sources_matching_excluded([self.SECRET, self.BAD], [], [])
        assert caught.value.unclassified == [self.BAD]
