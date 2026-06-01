"""CLI tests for ``mm tags`` (list / rename / delete / merge) — #688 PR3.

The service SQL surface (rename/delete/merge dry-run + apply, lock,
``updated_at`` policy, cache invalidation) is covered in
``test_services_tag_management.py`` and ``test_storage.py``. Here we mock
``services.tag_management`` and only pin the CLI glue: dry-run-by-default,
the ``--apply`` / ``--yes`` gate, the confirmation prompt, sample
rendering, error surfacing, and that ``search_pipeline`` is threaded into
the apply call.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from click.testing import CliRunner

from memtomem.cli import cli
from memtomem.services.tag_management import TagOpResult, TagOpSample

_STORAGE = object()
_PIPELINE = object()


def _comp():
    return SimpleNamespace(storage=_STORAGE, search_pipeline=_PIPELINE)


def _patch_bootstrap(monkeypatch, comp):
    @asynccontextmanager
    async def fake():
        yield comp

    monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", fake)


def _result(tag, affected, *, dry_run, with_sample=False):
    samples = ()
    if with_sample:
        samples = (
            TagOpSample(
                chunk_id=uuid4(),
                source_file="/tmp/a.md",
                content_preview="hello world",
                current_tags=("old", "keep"),
            ),
        )
    return TagOpResult(tag=tag, affected_chunks=affected, dry_run=dry_run, samples=samples)


# --------------------------------------------------------------------------- #
# list
# --------------------------------------------------------------------------- #
class TestTagsList:
    def test_list_renders_counts(self, monkeypatch):
        comp = _comp()
        comp.storage = SimpleNamespace(
            get_tag_counts=AsyncMock(return_value=[("python", 5), ("rust", 2)])
        )
        _patch_bootstrap(monkeypatch, comp)

        result = CliRunner().invoke(cli, ["tags", "list"])

        assert result.exit_code == 0, result.output
        assert "python  — 5 chunks" in result.output
        assert "rust  — 2 chunks" in result.output
        assert "2 tags across 7 chunk-tag assignments." in result.output

    def test_list_empty(self, monkeypatch):
        comp = _comp()
        comp.storage = SimpleNamespace(get_tag_counts=AsyncMock(return_value=[]))
        _patch_bootstrap(monkeypatch, comp)

        result = CliRunner().invoke(cli, ["tags", "list"])

        assert result.exit_code == 0, result.output
        assert "No tags found." in result.output


# --------------------------------------------------------------------------- #
# rename
# --------------------------------------------------------------------------- #
class TestTagsRename:
    def test_dry_run_default_does_not_apply(self, monkeypatch):
        mock = AsyncMock(return_value=_result("new", 3, dry_run=True, with_sample=True))
        monkeypatch.setattr("memtomem.services.tag_management.rename_tag", mock)
        _patch_bootstrap(monkeypatch, _comp())

        result = CliRunner().invoke(cli, ["tags", "rename", "old", "new"])

        assert result.exit_code == 0, result.output
        assert "Rename 'old' → 'new' would affect 3 chunks." in result.output
        assert "Sample affected chunks:" in result.output
        assert "Run with --apply" in result.output
        # Exactly one call, and it was the dry-run preview.
        mock.assert_awaited_once()
        assert mock.call_args_list[0].kwargs == {"dry_run": True}

    def test_apply_confirm_yes_writes(self, monkeypatch):
        mock = AsyncMock(
            side_effect=[
                _result("new", 3, dry_run=True),
                _result("new", 3, dry_run=False),
            ]
        )
        monkeypatch.setattr("memtomem.services.tag_management.rename_tag", mock)
        comp = _comp()
        _patch_bootstrap(monkeypatch, comp)

        result = CliRunner().invoke(cli, ["tags", "rename", "old", "new", "--apply"], input="y\n")

        assert result.exit_code == 0, result.output
        assert "Renamed 'old' → 'new' in 3 chunks." in result.output
        assert mock.await_count == 2
        # The apply call carries dry_run=False and the live search pipeline.
        assert mock.call_args_list[1].kwargs == {"dry_run": False, "search_pipeline": _PIPELINE}

    def test_apply_abort_on_no(self, monkeypatch):
        mock = AsyncMock(return_value=_result("new", 3, dry_run=True))
        monkeypatch.setattr("memtomem.services.tag_management.rename_tag", mock)
        _patch_bootstrap(monkeypatch, _comp())

        result = CliRunner().invoke(cli, ["tags", "rename", "old", "new", "--apply"], input="n\n")

        assert result.exit_code == 0, result.output
        assert "Aborted." in result.output
        # Preview only — no apply call.
        mock.assert_awaited_once()

    def test_apply_yes_non_interactive(self, monkeypatch):
        mock = AsyncMock(
            side_effect=[
                _result("new", 3, dry_run=True),
                _result("new", 3, dry_run=False),
            ]
        )
        monkeypatch.setattr("memtomem.services.tag_management.rename_tag", mock)
        _patch_bootstrap(monkeypatch, _comp())

        result = CliRunner().invoke(cli, ["tags", "rename", "old", "new", "--apply", "--yes"])

        assert result.exit_code == 0, result.output
        assert "Rename 'old' → 'new' across" not in result.output  # no prompt prose
        assert "Renamed 'old' → 'new' in 3 chunks." in result.output
        assert mock.await_count == 2

    def test_zero_affected_skips_apply(self, monkeypatch):
        mock = AsyncMock(return_value=_result("new", 0, dry_run=True))
        monkeypatch.setattr("memtomem.services.tag_management.rename_tag", mock)
        _patch_bootstrap(monkeypatch, _comp())

        result = CliRunner().invoke(cli, ["tags", "rename", "old", "new", "--apply", "--yes"])

        assert result.exit_code == 0, result.output
        assert "Nothing to do." in result.output
        # Even with --apply --yes, a 0-row preview never reaches the write call.
        mock.assert_awaited_once()

    def test_service_value_error_surfaces(self, monkeypatch):
        mock = AsyncMock(side_effect=ValueError("rename_tag old and new tag names are identical"))
        monkeypatch.setattr("memtomem.services.tag_management.rename_tag", mock)
        _patch_bootstrap(monkeypatch, _comp())

        result = CliRunner().invoke(cli, ["tags", "rename", "x", "x"])

        assert result.exit_code != 0
        assert "identical" in result.output

    def test_yes_without_apply_is_usage_error(self):
        result = CliRunner().invoke(cli, ["tags", "rename", "a", "b", "--yes"])

        assert result.exit_code != 0
        assert "--yes requires --apply" in result.output


# --------------------------------------------------------------------------- #
# delete
# --------------------------------------------------------------------------- #
class TestTagsDelete:
    def test_dry_run_default(self, monkeypatch):
        mock = AsyncMock(return_value=_result("dead", 4, dry_run=True, with_sample=True))
        monkeypatch.setattr("memtomem.services.tag_management.delete_tag", mock)
        _patch_bootstrap(monkeypatch, _comp())

        result = CliRunner().invoke(cli, ["tags", "delete", "dead"])

        assert result.exit_code == 0, result.output
        assert "Delete 'dead' would affect 4 chunks." in result.output
        assert "Run with --apply" in result.output
        mock.assert_awaited_once()

    def test_apply_yes_writes_with_pipeline(self, monkeypatch):
        mock = AsyncMock(
            side_effect=[
                _result("dead", 4, dry_run=True),
                _result("dead", 4, dry_run=False),
            ]
        )
        monkeypatch.setattr("memtomem.services.tag_management.delete_tag", mock)
        _patch_bootstrap(monkeypatch, _comp())

        result = CliRunner().invoke(cli, ["tags", "delete", "dead", "--apply", "--yes"])

        assert result.exit_code == 0, result.output
        assert "Removed 'dead' from 4 chunks." in result.output
        assert mock.call_args_list[1].kwargs == {"dry_run": False, "search_pipeline": _PIPELINE}

    def test_yes_without_apply_is_usage_error(self):
        result = CliRunner().invoke(cli, ["tags", "delete", "dead", "--yes"])

        assert result.exit_code != 0
        assert "--yes requires --apply" in result.output


# --------------------------------------------------------------------------- #
# merge
# --------------------------------------------------------------------------- #
class TestTagsMerge:
    def test_dry_run_default(self, monkeypatch):
        mock = AsyncMock(return_value=_result("python", 6, dry_run=True))
        monkeypatch.setattr("memtomem.services.tag_management.merge_tags", mock)
        _patch_bootstrap(monkeypatch, _comp())

        result = CliRunner().invoke(cli, ["tags", "merge", "py", "python3", "--into", "python"])

        assert result.exit_code == 0, result.output
        assert "Merge 'py', 'python3' → 'python' would affect 6 chunks." in result.output
        assert "Run with --apply" in result.output
        # The service receives the raw source list and the target.
        assert mock.call_args_list[0].args[1] == ["py", "python3"]
        assert mock.call_args_list[0].args[2] == "python"

    def test_apply_yes_writes(self, monkeypatch):
        mock = AsyncMock(
            side_effect=[
                _result("python", 6, dry_run=True),
                _result("python", 6, dry_run=False),
            ]
        )
        monkeypatch.setattr("memtomem.services.tag_management.merge_tags", mock)
        _patch_bootstrap(monkeypatch, _comp())

        result = CliRunner().invoke(
            cli, ["tags", "merge", "py", "python3", "--into", "python", "--apply", "--yes"]
        )

        assert result.exit_code == 0, result.output
        assert "Merged 'py', 'python3' → 'python' across 6 chunks." in result.output
        assert mock.call_args_list[1].kwargs == {"dry_run": False, "search_pipeline": _PIPELINE}

    def test_zero_affected_nothing_to_do(self, monkeypatch):
        mock = AsyncMock(return_value=_result("python", 0, dry_run=True))
        monkeypatch.setattr("memtomem.services.tag_management.merge_tags", mock)
        _patch_bootstrap(monkeypatch, _comp())

        result = CliRunner().invoke(
            cli, ["tags", "merge", "python", "--into", "python", "--apply", "--yes"]
        )

        assert result.exit_code == 0, result.output
        # Target-only collapse: chunks carrying 'python' exist, so the message
        # must not claim no chunk carries it — only that nothing would change.
        assert "Nothing to merge into 'python' — no chunks would change." in result.output
        assert "No chunks carry" not in result.output
        mock.assert_awaited_once()

    def test_target_required(self):
        result = CliRunner().invoke(cli, ["tags", "merge", "py"])

        assert result.exit_code != 0
        assert "into" in result.output.lower()
