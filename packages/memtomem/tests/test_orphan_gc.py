"""Tests for orphan project-tier chunk GC (ADR-0011 follow-up #884).

Two layers, kept separate on purpose:

* :class:`TestStorageHelpers` — exercise the SQL through a real
  :class:`SqliteBackend` so we catch sidecar/meta drift that
  ``AsyncMock`` fixtures would silently mask (memory:
  ``feedback_mocked_storage_hides_sql_bugs``).
* :class:`TestCli` — drives ``mm gc orphan-projects`` via Click's
  ``CliRunner`` with a mocked Components object to pin the dry-run /
  ``--apply`` / ``--apply --yes`` flow without booting full storage.
"""

from __future__ import annotations

import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from click.testing import CliRunner

from memtomem.cli import cli
from memtomem.config import StorageConfig
from memtomem.storage.orphan_gc import (
    OrphanProjectReport,
    SweepResult,
    find_orphan_project_roots,
    sweep_orphan_project_root,
)
from memtomem.storage.orphan_detect import OrphanScanResult
from memtomem.storage.sqlite_backend import SqliteBackend


_SHARED = "project_shared"
_LOCAL = "project_local"
_NOW = "2026-05-11T00:00:00"


@pytest.fixture
async def backend(tmp_path):
    """Real ``SqliteBackend`` (no vec table — ``dimension=0``)."""
    cfg = StorageConfig(sqlite_path=tmp_path / "orphan.db")
    be = SqliteBackend(
        config=cfg,
        dimension=0,
        embedding_provider="none",
        embedding_model="",
    )
    await be.initialize()
    yield be
    await be.close()


def _insert_chunk(
    backend: SqliteBackend,
    *,
    chunk_id: str,
    source_file: str,
    scope: str,
    project_root: str | None,
) -> int:
    """Insert one ``chunks`` row + matching ``chunks_fts`` row. Returns rowid."""
    db = backend._get_db()
    db.execute(
        "INSERT INTO chunks (id, content, content_hash, source_file, "
        "namespace, tags, created_at, updated_at, scope, project_root) "
        "VALUES (?, ?, ?, ?, 'default', '[]', ?, ?, ?, ?)",
        (
            chunk_id,
            f"body {chunk_id}",
            chunk_id,  # unique content_hash to avoid UNIQUE collision
            source_file,
            _NOW,
            _NOW,
            scope,
            project_root,
        ),
    )
    rowid = db.execute("SELECT rowid FROM chunks WHERE id = ?", (chunk_id,)).fetchone()[0]
    db.execute(
        "INSERT INTO chunks_fts (rowid, content, source_file) VALUES (?, ?, ?)",
        (rowid, f"body {chunk_id}", source_file),
    )
    db.commit()
    return rowid


def _set_ai_summary(backend: SqliteBackend, source_file: str) -> None:
    db = backend._get_db()
    db.execute(
        "INSERT OR REPLACE INTO _memtomem_meta (key, value) VALUES (?, ?)",
        (f"ai_summary:{source_file}", '{"summary": "stub", "language": "en"}'),
    )
    db.commit()


def _row_count(backend: SqliteBackend, table: str) -> int:
    return backend._get_db().execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _meta_count(backend: SqliteBackend, prefix: str = "ai_summary:") -> int:
    return (
        backend._get_db()
        .execute(
            "SELECT COUNT(*) FROM _memtomem_meta WHERE key LIKE ?",
            (f"{prefix}%",),
        )
        .fetchone()[0]
    )


class TestStorageHelpers:
    """SQL-side guarantees against a real SQLite backend."""

    @pytest.mark.asyncio
    async def test_find_returns_only_missing_roots(self, backend, tmp_path):
        live = tmp_path / "live"
        dead = tmp_path / "dead"
        live.mkdir()
        dead.mkdir()
        _insert_chunk(
            backend,
            chunk_id="11111111-1111-1111-1111-111111111111",
            source_file=str(live / "a.md"),
            scope=_SHARED,
            project_root=str(live),
        )
        _insert_chunk(
            backend,
            chunk_id="22222222-2222-2222-2222-222222222222",
            source_file=str(dead / "b.md"),
            scope=_SHARED,
            project_root=str(dead),
        )
        # Delete only ``dead`` — ``live`` should be ignored.
        shutil.rmtree(dead)

        reports = find_orphan_project_roots(backend._get_db())

        assert [r.project_root for r in reports] == [str(dead)]
        assert reports[0].total_rows == 1
        assert reports[0].scope_counts == {_SHARED: 1}
        assert reports[0].sample_source_files == (str(dead / "b.md"),)

    @pytest.mark.asyncio
    async def test_find_ignores_user_scope_rows(self, backend, tmp_path):
        """User-scope rows with stray ``project_root`` must NOT appear (ADR-0011 §8)."""
        dead = tmp_path / "dead"
        dead.mkdir()
        # Stray user-scope row whose project_root happens to be set.
        _insert_chunk(
            backend,
            chunk_id="33333333-3333-3333-3333-333333333333",
            source_file=str(dead / "u.md"),
            scope="user",
            project_root=str(dead),
        )
        shutil.rmtree(dead)

        reports = find_orphan_project_roots(backend._get_db())

        assert reports == []

    @pytest.mark.asyncio
    async def test_find_returns_empty_when_no_project_rows(self, backend, tmp_path):
        _insert_chunk(
            backend,
            chunk_id="44444444-4444-4444-4444-444444444444",
            source_file=str(tmp_path / "n.md"),
            scope="user",
            project_root=None,
        )
        assert find_orphan_project_roots(backend._get_db()) == []

    @pytest.mark.asyncio
    async def test_find_aggregates_scopes_per_root(self, backend, tmp_path):
        dead = tmp_path / "dead"
        dead.mkdir()
        _insert_chunk(
            backend,
            chunk_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            source_file=str(dead / "shared.md"),
            scope=_SHARED,
            project_root=str(dead),
        )
        _insert_chunk(
            backend,
            chunk_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            source_file=str(dead / "local.md"),
            scope=_LOCAL,
            project_root=str(dead),
        )
        shutil.rmtree(dead)

        reports = find_orphan_project_roots(backend._get_db())

        assert len(reports) == 1
        assert reports[0].scope_counts == {_SHARED: 1, _LOCAL: 1}
        assert reports[0].total_rows == 2

    @pytest.mark.asyncio
    async def test_sweep_cleans_all_four_surfaces(self, backend, tmp_path):
        """Pin: chunks, chunks_fts, and ai_summary all gone after sweep."""
        dead = tmp_path / "dead"
        dead.mkdir()
        source = str(dead / "doc.md")
        _insert_chunk(
            backend,
            chunk_id="55555555-5555-5555-5555-555555555555",
            source_file=source,
            scope=_SHARED,
            project_root=str(dead),
        )
        _set_ai_summary(backend, source)
        assert await backend.hold_source(Path(source), "source_missing")
        shutil.rmtree(dead)

        assert _row_count(backend, "chunks") == 1
        assert _row_count(backend, "chunks_fts") == 1
        assert _meta_count(backend) == 1

        result = sweep_orphan_project_root(
            backend._get_db(),
            str(dead),
            has_vec_table=backend._has_vec_table,
        )

        assert result.chunks_deleted == 1
        assert result.fts_deleted == 1
        assert result.ai_summaries_deleted == 1
        assert _row_count(backend, "chunks") == 0
        assert _row_count(backend, "chunks_fts") == 0
        assert _meta_count(backend) == 0
        assert not await backend.is_source_held(Path(source))

    @pytest.mark.asyncio
    async def test_sweep_does_not_touch_other_project_roots(self, backend, tmp_path):
        keep = tmp_path / "keep"
        drop = tmp_path / "drop"
        keep.mkdir()
        drop.mkdir()
        _insert_chunk(
            backend,
            chunk_id="66666666-6666-6666-6666-666666666666",
            source_file=str(keep / "k.md"),
            scope=_SHARED,
            project_root=str(keep),
        )
        _insert_chunk(
            backend,
            chunk_id="77777777-7777-7777-7777-777777777777",
            source_file=str(drop / "d.md"),
            scope=_SHARED,
            project_root=str(drop),
        )

        result = sweep_orphan_project_root(
            backend._get_db(),
            str(drop),
            has_vec_table=False,
        )

        assert result.chunks_deleted == 1
        # ``keep`` rows still present.
        assert _row_count(backend, "chunks") == 1
        assert _row_count(backend, "chunks_fts") == 1
        roots_left = backend._get_db().execute("SELECT project_root FROM chunks").fetchall()
        assert roots_left == [(str(keep),)]

    @pytest.mark.asyncio
    async def test_sweep_does_not_touch_user_scope_rows(self, backend, tmp_path):
        """ADR-0011 boundary: even with matching project_root, user-scope is off-limits."""
        dead = tmp_path / "dead"
        dead.mkdir()
        # Stray user-scope row with the same project_root value.
        _insert_chunk(
            backend,
            chunk_id="88888888-8888-8888-8888-888888888888",
            source_file=str(dead / "user.md"),
            scope="user",
            project_root=str(dead),
        )
        _insert_chunk(
            backend,
            chunk_id="99999999-9999-9999-9999-999999999999",
            source_file=str(dead / "proj.md"),
            scope=_SHARED,
            project_root=str(dead),
        )

        result = sweep_orphan_project_root(
            backend._get_db(),
            str(dead),
            has_vec_table=False,
        )

        assert result.chunks_deleted == 1  # only the project_shared row
        scopes_left = {
            row[0] for row in backend._get_db().execute("SELECT scope FROM chunks").fetchall()
        }
        assert scopes_left == {"user"}

    @pytest.mark.asyncio
    async def test_sweep_keeps_ai_summary_when_other_chunks_remain(self, backend, tmp_path):
        """Defensive AI-summary check: don't drop the cache if the source still has chunks."""
        dead = tmp_path / "dead"
        dead.mkdir()
        shared_source = str(dead / "shared.md")
        # Same source_file, one project_shared (will be swept) + one user (will survive).
        _insert_chunk(
            backend,
            chunk_id="cccccccc-cccc-cccc-cccc-cccccccccccc",
            source_file=shared_source,
            scope=_SHARED,
            project_root=str(dead),
        )
        _insert_chunk(
            backend,
            chunk_id="dddddddd-dddd-dddd-dddd-dddddddddddd",
            source_file=shared_source,
            scope="user",
            project_root=None,
        )
        _set_ai_summary(backend, shared_source)

        result = sweep_orphan_project_root(
            backend._get_db(),
            str(dead),
            has_vec_table=False,
        )

        assert result.chunks_deleted == 1
        # User-scope chunk still references this source → summary must stay.
        assert result.ai_summaries_deleted == 0
        assert _meta_count(backend) == 1

    @pytest.mark.asyncio
    async def test_sweep_unknown_root_is_noop(self, backend, tmp_path):
        result = sweep_orphan_project_root(
            backend._get_db(),
            str(tmp_path / "never-existed"),
            has_vec_table=False,
        )
        assert result == SweepResult(
            project_root=str(tmp_path / "never-existed"),
            chunks_deleted=0,
            fts_deleted=0,
            vec_deleted=0,
            ai_summaries_deleted=0,
        )

    @pytest.mark.asyncio
    async def test_backend_async_wrappers(self, backend, tmp_path):
        """Cover the ``SqliteBackend`` async wrappers used by the CLI."""
        dead = tmp_path / "dead"
        dead.mkdir()
        _insert_chunk(
            backend,
            chunk_id="eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
            source_file=str(dead / "x.md"),
            scope=_SHARED,
            project_root=str(dead),
        )
        shutil.rmtree(dead)

        reports = await backend.find_orphan_project_roots()
        assert len(reports) == 1
        assert reports[0].project_root == str(dead)

        result = await backend.sweep_orphan_project_root(str(dead))
        assert result.chunks_deleted == 1
        assert (await backend.find_orphan_project_roots()) == []


# ---------------------------------------------------------------------------
# CLI tests — mocked storage. The SQL surface is covered above; here we
# only pin the dry-run / --apply / --yes branching.
# ---------------------------------------------------------------------------


def _mock_components(reports, sweep_result=None):
    storage = SimpleNamespace(
        find_orphan_project_roots=AsyncMock(return_value=reports),
        sweep_orphan_project_root=AsyncMock(
            return_value=sweep_result
            or SweepResult(
                "/tmp/dead", chunks_deleted=2, fts_deleted=2, vec_deleted=0, ai_summaries_deleted=1
            )
        ),
    )
    return SimpleNamespace(
        storage=storage,
        config=SimpleNamespace(indexing=SimpleNamespace(retain_missing_dirs=[])),
    )


def _patch_bootstrap(monkeypatch, comp):
    @asynccontextmanager
    async def fake():
        yield comp

    monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", fake)


class TestCli:
    def test_no_orphans_message(self, monkeypatch):
        comp = _mock_components([])
        _patch_bootstrap(monkeypatch, comp)

        result = CliRunner().invoke(cli, ["gc", "orphan-projects"])

        assert result.exit_code == 0
        assert "No orphan project_root entries found." in result.output
        comp.storage.sweep_orphan_project_root.assert_not_awaited()

    def test_dry_run_default_does_not_delete(self, monkeypatch):
        report = OrphanProjectReport(
            project_root="/tmp/dead",
            total_rows=3,
            scope_counts={_SHARED: 2, _LOCAL: 1},
            sample_source_files=("/tmp/dead/a.md", "/tmp/dead/b.md"),
        )
        comp = _mock_components([report])
        _patch_bootstrap(monkeypatch, comp)

        result = CliRunner().invoke(cli, ["gc", "orphan-projects"])

        assert result.exit_code == 0
        assert "/tmp/dead" in result.output
        assert "project_shared=2" in result.output
        assert "project_local=1" in result.output
        assert "/tmp/dead/a.md" in result.output
        assert "Run with --apply" in result.output
        comp.storage.sweep_orphan_project_root.assert_not_awaited()

    def test_apply_requires_confirmation(self, monkeypatch):
        """Without --yes, --apply prompts per root and skips on 'n'."""
        report = OrphanProjectReport(
            project_root="/tmp/dead",
            total_rows=2,
            scope_counts={_SHARED: 2},
            sample_source_files=(),
        )
        comp = _mock_components([report])
        _patch_bootstrap(monkeypatch, comp)

        result = CliRunner().invoke(cli, ["gc", "orphan-projects", "--apply"], input="n\n")

        assert result.exit_code == 0, result.output
        assert "Delete 2 chunks under /tmp/dead?" in result.output
        assert "skipped: /tmp/dead" in result.output
        comp.storage.sweep_orphan_project_root.assert_not_awaited()

    def test_apply_yes_deletes_non_interactively(self, monkeypatch):
        report = OrphanProjectReport(
            project_root="/tmp/dead",
            total_rows=2,
            scope_counts={_SHARED: 2},
            sample_source_files=(),
        )
        comp = _mock_components([report])
        _patch_bootstrap(monkeypatch, comp)

        result = CliRunner().invoke(cli, ["gc", "orphan-projects", "--apply", "--yes"])

        assert result.exit_code == 0, result.output
        # No prompt prose when --yes is set.
        assert "Delete 2 chunks under" not in result.output
        comp.storage.sweep_orphan_project_root.assert_awaited_once_with("/tmp/dead", keep=None)
        assert "Done: 2 chunks across 1 project root" in result.output

    def test_yes_without_apply_is_usage_error(self, monkeypatch):
        # The --yes flag is meaningless without --apply; the command
        # should refuse rather than silently degrade.
        result = CliRunner().invoke(cli, ["gc", "orphan-projects", "--yes"])

        assert result.exit_code != 0
        assert "--yes requires --apply" in result.output


class TestOrphanSourcesCli:
    def _patch_scan(self, monkeypatch, result: OrphanScanResult) -> None:
        monkeypatch.setattr(
            "memtomem.storage.orphan_detect.scan_orphans",
            AsyncMock(return_value=result),
        )

    @staticmethod
    def _comp(roots=(), deleted=0, invalidate=None):
        return SimpleNamespace(
            storage=SimpleNamespace(delete_by_source=AsyncMock(return_value=deleted)),
            search_pipeline=SimpleNamespace(invalidate_cache=invalidate or Mock()),
            config=SimpleNamespace(
                indexing=SimpleNamespace(
                    all_index_roots=lambda: list(roots), retain_missing_dirs=[]
                )
            ),
        )

    @staticmethod
    def _deleted(comp) -> list[Path]:
        return [c.args[0] for c in comp.storage.delete_by_source.await_args_list]

    def test_dry_run_lists_without_deleting(self, monkeypatch, tmp_path):
        missing = tmp_path / "missing.md"
        comp = self._comp()
        _patch_bootstrap(monkeypatch, comp)
        self._patch_scan(
            monkeypatch,
            OrphanScanResult(3, 1, [missing]),
        )

        result = CliRunner().invoke(cli, ["gc", "orphan-sources"])

        assert result.exit_code == 0, result.output
        assert str(missing) in result.output
        assert "Run with --apply" in result.output
        comp.storage.delete_by_source.assert_not_awaited()

    def test_dry_run_groups_by_owning_root(self, monkeypatch, tmp_path):
        """One header per root, the outside-every-root group last (#2833)."""
        plans = tmp_path / "plans"
        notes = tmp_path / "notes"
        orphans = [notes / "n.md", tmp_path / "wt" / "w.md", plans / "b.md", plans / "a.md"]
        comp = self._comp(roots=[plans, notes])
        _patch_bootstrap(monkeypatch, comp)
        self._patch_scan(monkeypatch, OrphanScanResult(10, 4, orphans))

        result = CliRunner().invoke(cli, ["gc", "orphan-sources"])

        assert result.exit_code == 0, result.output
        lines = result.output.splitlines()
        start = lines.index(f"{notes} (1):")
        assert lines[start : start + 9] == [
            f"{notes} (1):",
            f"  - {notes / 'n.md'}",
            "",
            f"{plans} (2):",
            f"  - {plans / 'a.md'}",
            f"  - {plans / 'b.md'}",
            "",
            "outside every configured root (1):",
            f"  - {tmp_path / 'wt' / 'w.md'}",
        ]
        assert "Add --under DIR" in result.output
        comp.storage.delete_by_source.assert_not_awaited()

    def test_apply_requires_confirmation(self, monkeypatch, tmp_path):
        """--apply without --yes prompts, and the default answer is No.

        Blank input (just Enter) must delete nothing — the doctor
        stale_source finding now points users straight at ``--apply``
        (#1928), so this default-No gate is the remaining safety net.
        """
        missing = tmp_path / "missing.md"
        invalidate = Mock()
        comp = self._comp(roots=[tmp_path], invalidate=invalidate)
        _patch_bootstrap(monkeypatch, comp)
        self._patch_scan(
            monkeypatch,
            OrphanScanResult(1, 1, [missing]),
        )

        result = CliRunner().invoke(cli, ["gc", "orphan-sources", "--apply"], input="\n")

        assert result.exit_code == 0, result.output
        assert f"Delete indexed chunks for 1 missing source file(s) under {tmp_path}?" in (
            result.output
        )
        assert "Cleanup cancelled; no chunks were deleted (1 group(s) declined)." in result.output
        comp.storage.delete_by_source.assert_not_awaited()
        invalidate.assert_not_called()

    def test_apply_confirms_each_group(self, monkeypatch, tmp_path):
        """Declining one group keeps its sources; the others are deleted (#2833)."""
        plans = tmp_path / "plans"
        wt = tmp_path / "wt" / "w.md"
        invalidate = Mock()
        comp = self._comp(roots=[plans], deleted=3, invalidate=invalidate)
        _patch_bootstrap(monkeypatch, comp)
        self._patch_scan(monkeypatch, OrphanScanResult(5, 2, [plans / "p.md", wt]))

        result = CliRunner().invoke(cli, ["gc", "orphan-sources", "--apply"], input="n\ny\n")

        assert result.exit_code == 0, result.output
        assert f"under {plans}?" in result.output
        assert "missing source file(s) outside every configured root?" in result.output
        assert f"  skipped: {plans}" in result.output
        assert self._deleted(comp) == [wt]
        invalidate.assert_called_once_with()
        assert "3 chunks deleted from 1 sources; 1 group(s) declined." in result.output

    def test_abort_after_an_accepted_group_still_invalidates(self, monkeypatch, tmp_path):
        """EOF at a later prompt aborts, but the earlier group's deletes are not cached."""
        a = tmp_path / "a"
        b = tmp_path / "b"
        invalidate = Mock()
        comp = self._comp(roots=[a, b], deleted=2, invalidate=invalidate)
        _patch_bootstrap(monkeypatch, comp)
        self._patch_scan(monkeypatch, OrphanScanResult(5, 2, [a / "1.md", b / "2.md"]))

        result = CliRunner().invoke(cli, ["gc", "orphan-sources", "--apply"], input="y\n")

        assert result.exit_code != 0
        assert self._deleted(comp) == [a / "1.md"]
        invalidate.assert_called_once_with()
        assert "Stopped: 2 chunks deleted from 1 sources." in result.output

    def test_declining_every_group_reports_the_count(self, monkeypatch, tmp_path):
        a = tmp_path / "a"
        b = tmp_path / "b"
        comp = self._comp(roots=[a, b])
        _patch_bootstrap(monkeypatch, comp)
        self._patch_scan(monkeypatch, OrphanScanResult(5, 2, [a / "1.md", b / "2.md"]))

        result = CliRunner().invoke(cli, ["gc", "orphan-sources", "--apply"], input="n\nn\n")

        assert result.exit_code == 0, result.output
        assert "Cleanup cancelled; no chunks were deleted (2 group(s) declined)." in result.output
        comp.storage.delete_by_source.assert_not_awaited()

    @pytest.mark.parametrize("blank", ["", "  "])
    def test_blank_under_is_refused(self, monkeypatch, tmp_path, blank):
        """``Path("")`` is the cwd; an unset ``$DIR`` must not widen the purge."""
        comp = self._comp(roots=[tmp_path])
        _patch_bootstrap(monkeypatch, comp)
        self._patch_scan(monkeypatch, OrphanScanResult(1, 1, [tmp_path / "a.md"]))
        monkeypatch.chdir(tmp_path)

        result = CliRunner().invoke(
            cli, ["gc", "orphan-sources", "--apply", "--yes", "--under", blank]
        )

        assert result.exit_code == 2
        assert "must name a directory" in result.output
        comp.storage.delete_by_source.assert_not_awaited()

    def test_apply_yes_deletes_and_invalidates(self, monkeypatch, tmp_path):
        missing = tmp_path / "missing.md"
        invalidate = Mock()
        comp = self._comp(deleted=4, invalidate=invalidate)
        _patch_bootstrap(monkeypatch, comp)
        self._patch_scan(
            monkeypatch,
            OrphanScanResult(1, 1, [missing]),
        )

        result = CliRunner().invoke(cli, ["gc", "orphan-sources", "--apply", "--yes"])

        assert result.exit_code == 0, result.output
        comp.storage.delete_by_source.assert_awaited_once_with(missing)
        invalidate.assert_called_once_with()
        assert "4 chunks deleted" in result.output

    def test_under_limits_apply_yes(self, monkeypatch, tmp_path):
        """--apply --yes --under deletes only the named directories (#2833)."""
        plans = tmp_path / "plans"
        wt = tmp_path / "repo" / ".worktrees"
        orphans = [plans / "p.md", wt / "x" / "a.md", tmp_path / "t" / "b.md"]
        comp = self._comp(roots=[plans], deleted=1)
        _patch_bootstrap(monkeypatch, comp)
        self._patch_scan(monkeypatch, OrphanScanResult(9, 3, orphans))

        preview = CliRunner().invoke(
            cli, ["gc", "orphan-sources", "--under", str(wt), "--under", str(tmp_path / "t")]
        )
        result = CliRunner().invoke(
            cli,
            ["gc", "orphan-sources", "--apply", "--yes", "--under", str(wt)],
        )

        assert preview.exit_code == 0, preview.output
        assert str(plans / "p.md") not in preview.output
        assert "1 more orphaned source(s) are outside the given directories." in preview.output
        assert result.exit_code == 0, result.output
        assert self._deleted(comp) == [wt / "x" / "a.md"]

    def test_under_matching_nothing_deletes_nothing(self, monkeypatch, tmp_path):
        comp = self._comp(roots=[tmp_path])
        _patch_bootstrap(monkeypatch, comp)
        self._patch_scan(
            monkeypatch, OrphanScanResult(4, 2, [tmp_path / "a.md", tmp_path / "b.md"])
        )

        result = CliRunner().invoke(
            cli,
            ["gc", "orphan-sources", "--apply", "--yes", "--under", str(tmp_path / "other")],
        )

        assert result.exit_code == 0, result.output
        assert "No orphaned sources under the given directories (2 orphaned elsewhere)." in (
            result.output
        )
        comp.storage.delete_by_source.assert_not_awaited()

    def test_a_retained_source_is_kept_out_of_the_purge(self, monkeypatch, tmp_path):
        """#2833: an archive under ``retain_missing_dirs`` is neither listed nor deleted."""
        root = tmp_path.resolve()
        kept = root / "plans" / "a.md"
        gone = root / "notes" / "b.md"
        comp = self._comp(roots=[root], deleted=1)
        comp.config.indexing.retain_missing_dirs = [root / "plans"]
        _patch_bootstrap(monkeypatch, comp)
        self._patch_scan(monkeypatch, OrphanScanResult(5, 2, [kept, gone]))

        result = CliRunner().invoke(cli, ["gc", "orphan-sources", "--apply", "--yes"])

        assert result.exit_code == 0, result.output
        assert str(kept) not in result.output
        assert "1 archived source(s) under retain_missing_dirs are kept." in result.output
        assert self._deleted(comp) == [gone]

    def test_yes_requires_apply(self):
        result = CliRunner().invoke(cli, ["gc", "orphan-sources", "--yes"])
        assert result.exit_code != 0
        assert "--yes requires --apply" in result.output


class TestOrphanProjectsKeepRetained:
    """#2833: a retained source under a missing project root is never swept."""

    async def test_kept_rows_are_reported_and_survive_the_sweep(self, backend):
        dead = "/tmp/dead-project-2833"
        _insert_chunk(
            backend,
            chunk_id="k1",
            source_file=f"{dead}/plans/keep.md",
            scope=_SHARED,
            project_root=dead,
        )
        _insert_chunk(
            backend,
            chunk_id="g1",
            source_file=f"{dead}/notes/gone.md",
            scope=_SHARED,
            project_root=dead,
        )

        def keep(source: str) -> bool:
            return source.startswith(f"{dead}/plans/")

        (report,) = await backend.find_orphan_project_roots(keep=keep)
        assert (report.total_rows, report.kept_rows) == (1, 1)
        assert report.sample_source_files == (f"{dead}/notes/gone.md",)

        result = await backend.sweep_orphan_project_root(dead, keep=keep)

        assert result.chunks_deleted == 1
        rows = backend._get_db().execute("SELECT id FROM chunks ORDER BY id").fetchall()
        assert [r[0] for r in rows] == ["k1"]

    async def test_a_root_with_only_retained_rows_has_nothing_to_sweep(self, backend):
        dead = "/tmp/dead-project-2833b"
        _insert_chunk(
            backend,
            chunk_id="k1",
            source_file=f"{dead}/keep.md",
            scope=_LOCAL,
            project_root=dead,
        )

        (report,) = await backend.find_orphan_project_roots(keep=lambda s: True)

        assert (report.total_rows, report.kept_rows, report.scope_counts) == (0, 1, {})
        assert (
            await backend.sweep_orphan_project_root(dead, keep=lambda s: True)
        ).chunks_deleted == 0


class TestOrphanSourcesUnderRealStorage:
    @pytest.mark.requires_symlinks
    async def test_under_judges_a_relinked_source_where_its_purge_lands(
        self, backend, monkeypatch, tmp_path
    ):
        """A stored path that became a dangling symlink is judged by its target.

        ``delete_by_source`` deletes by the resolved path, so a purge of
        ``work/a.md`` here would delete ``plans/b.md``'s chunks. ``--under
        work`` must therefore not select it, or the purge reaches a directory
        the user left out (#2833).
        """
        from memtomem.cli import gc_cmd
        from memtomem.storage import orphan_detect

        monkeypatch.setattr(orphan_detect, "ORPHAN_RECHECK_DELAY_SECONDS", 0.0)
        base = tmp_path.resolve()
        work, plans = base / "work", base / "plans"
        work.mkdir()
        plans.mkdir()
        a, b = work / "a.md", plans / "b.md"
        _insert_chunk(backend, chunk_id="a", source_file=str(a), scope="user", project_root=None)
        _insert_chunk(backend, chunk_id="b", source_file=str(b), scope="user", project_root=None)
        a.symlink_to(b)  # b does not exist: both sources are missing
        comp = SimpleNamespace(
            storage=backend,
            search_pipeline=SimpleNamespace(invalidate_cache=Mock()),
            config=SimpleNamespace(
                indexing=SimpleNamespace(
                    all_index_roots=lambda: [work, plans], retain_missing_dirs=[]
                )
            ),
        )
        _patch_bootstrap(monkeypatch, comp)

        await gc_cmd._run_orphan_sources(apply_=True, assume_yes=True, under=(work,))

        rows = backend._get_db().execute("SELECT id FROM chunks ORDER BY id").fetchall()
        assert [r[0] for r in rows] == ["a", "b"]
