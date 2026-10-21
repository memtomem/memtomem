"""Two-pass source availability and explicit orphan purge checks (#1565, #2498)."""

from __future__ import annotations

from pathlib import Path
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest import mock
from unittest.mock import AsyncMock, MagicMock

import pytest

from memtomem.storage.orphan_detect import (
    OrphanGroup,
    OrphanScanResult,
    group_orphans,
    is_suspected_mass_orphan,
    scan_orphans,
)


class _FakeSource:
    """A source path whose ``exists()`` returns a scripted True/False sequence.

    The first pass calls ``exists()`` on every source; the second pass calls it
    again only on first-pass failures. Once the scripted sequence is exhausted
    the last value repeats, so ``[True]`` = always present, ``[False, False]`` =
    stable orphan, ``[False, True]`` = transient (absent then back).
    """

    def __init__(self, name: str, exists_seq: list[bool]) -> None:
        self._name = name
        self._seq = list(exists_seq)
        self._last = self._seq[-1]

    def stat(self):
        if self._seq:
            self._last = self._seq.pop(0)
        if not self._last:
            raise FileNotFoundError(self._name)
        return None

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<src {self._name}>"


def _storage(sources) -> MagicMock:
    st = MagicMock()
    st.get_all_source_files = AsyncMock(return_value=set(sources))
    return st


class TestScanOrphans:
    @pytest.mark.asyncio
    async def test_permission_error_is_unavailable_not_purge_candidate(self, tmp_path):
        source = tmp_path / "offline.md"
        real_stat = Path.stat

        def unavailable(self, *args, **kwargs):
            if self == source:
                raise PermissionError("temporarily inaccessible")
            return real_stat(self, *args, **kwargs)

        with mock.patch.object(Path, "stat", autospec=True, side_effect=unavailable):
            result = await scan_orphans(_storage([source]), recheck_delay_seconds=0)

        assert result.confirmed_orphans == []
        assert result.unavailable_sources == [source]

    @pytest.mark.asyncio
    async def test_empty_sources(self):
        result = await scan_orphans(_storage([]), recheck_delay_seconds=0)
        assert result.total_sources == 0
        assert result.first_pass_orphans == 0
        assert result.confirmed_orphans == []
        assert result.ratio == 0.0

    @pytest.mark.asyncio
    async def test_all_present_no_second_pass(self):
        # No first-pass failures -> function returns before the delay/second
        # pass. ``first_pass_orphans == 0`` proves the early-return branch.
        sources = [_FakeSource("a", [True]), _FakeSource("b", [True])]
        result = await scan_orphans(_storage(sources), recheck_delay_seconds=0)
        assert result.total_sources == 2
        assert result.first_pass_orphans == 0
        assert result.confirmed_orphans == []

    @pytest.mark.asyncio
    async def test_transient_absence_excluded(self):
        transient = _FakeSource("flaky", [False, True])
        present = _FakeSource("ok", [True])
        result = await scan_orphans(_storage([transient, present]), recheck_delay_seconds=0)
        assert result.total_sources == 2
        assert result.first_pass_orphans == 1  # seen absent on pass 1
        assert result.confirmed_orphans == []  # but present on pass 2

    @pytest.mark.asyncio
    async def test_stable_orphan_confirmed(self):
        stable = _FakeSource("gone", [False, False])
        present = _FakeSource("ok", [True])
        result = await scan_orphans(_storage([stable, present]), recheck_delay_seconds=0)
        assert result.total_sources == 2
        assert result.first_pass_orphans == 1
        assert result.confirmed_orphans == [stable]

    @pytest.mark.asyncio
    async def test_mixed_transient_and_stable(self):
        stable = _FakeSource("gone", [False, False])
        transient = _FakeSource("flaky", [False, True])
        present = _FakeSource("ok", [True])
        result = await scan_orphans(_storage([stable, transient, present]), recheck_delay_seconds=0)
        assert result.total_sources == 3
        assert result.first_pass_orphans == 2
        assert result.confirmed_orphans == [stable]
        assert result.ratio == pytest.approx(1 / 3)


class TestMassOrphanBrake:
    def _result(self, confirmed: int, total: int) -> OrphanScanResult:
        return OrphanScanResult(
            total_sources=total,
            first_pass_orphans=confirmed,
            confirmed_orphans=[_FakeSource(str(i), [False, False]) for i in range(confirmed)],
        )

    def test_single_orphan_below_floor(self):
        # 1/1 is ratio 1.0 but below the absolute floor -> deleting the only
        # indexed file is normal, not a mass event.
        assert is_suspected_mass_orphan(self._result(1, 1)) is False

    def test_many_and_high_ratio_trips(self):
        assert is_suspected_mass_orphan(self._result(12, 12)) is True

    def test_many_but_low_ratio_ok(self):
        # 12 orphans but only 24% of a large corpus -> plausible real cleanup.
        assert is_suspected_mass_orphan(self._result(12, 50)) is False

    def test_exactly_at_thresholds_trips(self):
        # count == floor (10) and ratio == 0.5 -> both boundaries inclusive.
        assert is_suspected_mass_orphan(self._result(10, 20)) is True

    def test_below_floor_even_at_full_ratio(self):
        assert is_suspected_mass_orphan(self._result(9, 9)) is False


class TestInteractiveToolSkipsBrake:
    """Explicit purge may delete many confirmed-missing sources at once."""

    @pytest.mark.asyncio
    async def test_many_orphans_still_deleted(self, monkeypatch, tmp_path):
        from memtomem.server.tools import dedup_decay
        from memtomem.storage import orphan_detect

        monkeypatch.setattr(orphan_detect, "ORPHAN_RECHECK_DELAY_SECONDS", 0.0)

        # 12 stable orphans / 12 sources — well past the mass-delete brake that
        # the unattended paths enforce.
        missing = {tmp_path / f"gone-{i}.md" for i in range(12)}
        app = MagicMock()
        app.storage.get_all_source_files = AsyncMock(return_value=missing)
        app.storage.delete_by_source = AsyncMock(return_value=1)
        app.search_pipeline.invalidate_cache = MagicMock()

        async def _fake_app(_ctx):
            return app

        monkeypatch.setattr(dedup_decay, "_get_app_initialized", _fake_app)

        preview = await dedup_decay.mem_cleanup_orphans(dry_run=False, ctx=None)
        assert "confirm_purge=True" in preview
        app.storage.delete_by_source.assert_not_awaited()

        string_flag = await dedup_decay.mem_cleanup_orphans(
            dry_run=False,
            confirm_purge="false",
            ctx=None,  # type: ignore[arg-type]
        )
        assert "dry-run, no deletions" in string_flag
        string_dry_run = await dedup_decay.mem_cleanup_orphans(
            dry_run="false",
            confirm_purge=True,
            ctx=None,  # type: ignore[arg-type]
        )
        assert "dry-run, no deletions" in string_dry_run
        app.storage.delete_by_source.assert_not_awaited()

        out = await dedup_decay.mem_cleanup_orphans(dry_run=False, confirm_purge=True, ctx=None)

        assert "Cleanup complete" in out
        assert "Orphaned files: 12" in out
        assert app.storage.delete_by_source.await_count == 12  # brake NOT applied

    @pytest.mark.asyncio
    async def test_recovered_source_is_skipped_at_apply_time(self, monkeypatch, tmp_path):
        from memtomem.cli import _bootstrap, gc_cmd
        from memtomem.server.tools import dedup_decay
        from memtomem.storage import orphan_detect

        source = tmp_path / "returned.md"
        scan = OrphanScanResult(total_sources=1, first_pass_orphans=1, confirmed_orphans=[source])
        monkeypatch.setattr(orphan_detect, "scan_orphans", AsyncMock(return_value=scan))
        monkeypatch.setattr(dedup_decay, "scan_orphans", AsyncMock(return_value=scan))
        monkeypatch.setattr(orphan_detect, "source_state", lambda _path: "present")
        monkeypatch.setattr(dedup_decay, "source_state", lambda _path: "present")
        app = SimpleNamespace(
            storage=MagicMock(delete_by_source=AsyncMock(return_value=1)),
            search_pipeline=MagicMock(),
            config=SimpleNamespace(indexing=SimpleNamespace(all_index_roots=lambda: [tmp_path])),
        )

        @asynccontextmanager
        async def components():
            yield app

        monkeypatch.setattr(_bootstrap, "cli_components", components)
        await gc_cmd._run_orphan_sources(apply_=True, assume_yes=True)
        app.storage.delete_by_source.assert_not_awaited()

        async def fake_app(_ctx):
            return app

        monkeypatch.setattr(dedup_decay, "_get_app_initialized", fake_app)
        monkeypatch.setattr(
            dedup_decay, "capture_session_for_untracked_write", AsyncMock(return_value=None)
        )
        out = await dedup_decay.mem_cleanup_orphans(dry_run=False, confirm_purge=True, ctx=None)
        assert "Recovered or unavailable: 1" in out
        app.storage.delete_by_source.assert_not_awaited()


class TestGroupOrphans:
    """Grouping by owning root and the ``under`` filter of the explicit purge (#2833)."""

    def test_groups_by_most_specific_root_with_outside_last(self, tmp_path):
        work = tmp_path / "work"
        notes = work / "notes"
        plans = tmp_path / "plans"
        orphans = [
            notes / "b.md",
            tmp_path / "elsewhere" / "x.md",
            plans / "p.md",
            work / "w.md",
            notes / "a.md",
        ]

        groups, excluded = group_orphans(orphans, [work, notes, plans])

        assert excluded == 0
        assert groups == [
            OrphanGroup(plans, [plans / "p.md"]),
            OrphanGroup(work, [work / "w.md"]),
            OrphanGroup(notes, [notes / "a.md", notes / "b.md"]),
            OrphanGroup(None, [tmp_path / "elsewhere" / "x.md"]),
        ]
        assert groups[-1].label == "outside every configured root"

    def test_under_keeps_only_sources_below_the_directories(self, tmp_path):
        root = tmp_path / "root"
        wt = tmp_path / "repo" / ".worktrees"
        orphans = [root / "keep.md", wt / "a" / "gone.md", tmp_path / "tmpdir" / "t.md"]

        groups, excluded = group_orphans(orphans, [root], under=[wt, tmp_path / "tmpdir"])

        assert excluded == 1
        assert groups == [OrphanGroup(None, [wt / "a" / "gone.md", tmp_path / "tmpdir" / "t.md"])]

    def test_under_is_a_directory_not_a_string_prefix(self, tmp_path):
        orphans = [tmp_path / "plans-old" / "p.md", tmp_path / "plans" / "q.md"]

        groups, excluded = group_orphans(orphans, [], under=[tmp_path / "plans"])

        assert excluded == 1
        assert groups == [OrphanGroup(None, [tmp_path / "plans" / "q.md"])]

    @pytest.mark.requires_symlinks
    def test_under_matches_a_live_symlink_alias(self, tmp_path):
        real = tmp_path / "real"
        real.mkdir()
        alias = tmp_path / "alias"
        alias.symlink_to(real, target_is_directory=True)
        stored = real.resolve() / "gone.md"

        groups, excluded = group_orphans([stored], [], under=[alias])
        assert (excluded, groups) == (0, [OrphanGroup(None, [stored])])

        # Once the alias is gone nothing resolves it to the stored path; the
        # filter then selects nothing rather than everything.
        alias.unlink()
        groups, excluded = group_orphans([stored], [], under=[alias])
        assert (excluded, groups) == (1, [])

    def test_under_a_directory_that_no_longer_exists(self, tmp_path):
        gone = tmp_path / "removed-worktree"
        groups, excluded = group_orphans([gone / "f.md"], [], under=[gone])
        assert (excluded, groups) == (0, [OrphanGroup(None, [gone / "f.md"])])


class TestCleanupOrphansUnder:
    """``mem_cleanup_orphans`` groups its preview and takes ``under`` (#2833)."""

    @pytest.fixture
    def app(self, monkeypatch, tmp_path):
        from memtomem.server.tools import dedup_decay

        plans = tmp_path / "plans"
        self.plans = plans
        self.orphans = [plans / "p.md", tmp_path / "wt" / "a.md", tmp_path / "wt" / "b.md"]
        scan = OrphanScanResult(9, 3, list(self.orphans))
        monkeypatch.setattr(dedup_decay, "scan_orphans", AsyncMock(return_value=scan))
        monkeypatch.setattr(dedup_decay, "source_state", lambda _path: "missing")
        monkeypatch.setattr(
            dedup_decay, "capture_session_for_untracked_write", AsyncMock(return_value=None)
        )
        monkeypatch.setattr(dedup_decay, "flag_untracked_write", AsyncMock())
        app = SimpleNamespace(
            storage=MagicMock(delete_by_source=AsyncMock(return_value=2)),
            search_pipeline=MagicMock(),
            config=SimpleNamespace(indexing=SimpleNamespace(all_index_roots=lambda: [plans])),
        )

        async def fake_app(_ctx):
            return app

        monkeypatch.setattr(dedup_decay, "_get_app_initialized", fake_app)
        return app

    @pytest.mark.asyncio
    async def test_preview_is_grouped(self, app, tmp_path):
        from memtomem.server.tools import dedup_decay

        out = await dedup_decay.mem_cleanup_orphans(ctx=None)

        lines = out.splitlines()
        start = lines.index(f"{self.plans} (1):")
        assert lines[start : start + 6] == [
            f"{self.plans} (1):",
            f"  {self.plans / 'p.md'}",
            "",
            "outside every configured root (2):",
            f"  {tmp_path / 'wt' / 'a.md'}",
            f"  {tmp_path / 'wt' / 'b.md'}",
        ]
        app.storage.delete_by_source.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("as_list", [True, False])
    async def test_under_limits_the_purge(self, app, tmp_path, as_list):
        from memtomem.server.tools import dedup_decay

        wt = str(tmp_path / "wt")
        out = await dedup_decay.mem_cleanup_orphans(
            dry_run=False, confirm_purge=True, under=[wt] if as_list else wt, ctx=None
        )

        assert "Orphaned files: 2" in out
        deleted = [c.args[0] for c in app.storage.delete_by_source.await_args_list]
        assert deleted == [tmp_path / "wt" / "a.md", tmp_path / "wt" / "b.md"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", [[], "", "  ", [""], ["ok", 3], 5, {"d": "x"}])
    async def test_malformed_under_is_refused_not_unfiltered(self, app, bad):
        from memtomem.server.tools import dedup_decay

        out = await dedup_decay.mem_cleanup_orphans(
            dry_run=False, confirm_purge=True, under=bad, ctx=None
        )

        assert out.startswith("Error")
        assert "under must be" in out
        app.storage.delete_by_source.assert_not_awaited()

    @pytest.mark.parametrize("value", ["/some/dir", ["/a", "/b"], None])
    def test_mcp_boundary_accepts_a_path_or_a_list(self, value):
        """A direct MCP call is validated against the signature before the tool runs."""
        from mcp.server.mcpserver.utilities.func_metadata import func_metadata

        from memtomem.server.tools.dedup_decay import mem_cleanup_orphans

        model = func_metadata(mem_cleanup_orphans).arg_model
        assert model.model_validate({"under": value}).under == value
