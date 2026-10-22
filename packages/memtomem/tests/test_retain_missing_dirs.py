"""``indexing.retain_missing_dirs``: a vanished file's chunks stay searchable (#2833).

Some memory dirs belong to tools that delete their own files, and there the
indexed chunks are the only copy. Every absence path used to hold such a
source, taking it out of search. Under a retain dir a confirmed-missing source
is shown instead, and an absence hold placed before the dir was retained is
lifted. Holds for anything other than absence stay.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from helpers import StubCtx
from memtomem.config import IndexingConfig
from memtomem.indexing.watcher import FileWatcher
from memtomem.server.context import AppContext
from memtomem.server.health_checks import check_orphan_count
from memtomem.server.tools import dedup_decay, memory_crud
from memtomem.source_provenance import SOURCE_MISSING_DETAIL
from memtomem.storage import orphan_detect
from memtomem.storage.orphan_detect import RetainRule
from memtomem.storage.source_holds import REDACTION_BLOCKED_HOLD
from memtomem.storage.sqlite_helpers import norm_path

BODY = "## Plan\n\nretainedarchiveword lives only in this file\n"


@pytest.fixture
async def env(bm25_only_components, monkeypatch):
    comp, mem_dir = bm25_only_components
    monkeypatch.setattr(orphan_detect, "ORPHAN_RECHECK_DELAY_SECONDS", 0.0)
    comp.config.indexing.retain_missing_dirs = [mem_dir]
    return comp, mem_dir


async def _indexed(comp, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(BODY, encoding="utf-8")
    stats = await comp.index_engine.index_file(path)
    assert stats.indexed_chunks == 1, stats
    return path


async def _hidden(comp, path: Path) -> bool:
    return await comp.storage.is_source_hidden(Path(norm_path(path)))


async def _hits(comp) -> int:
    comp.search_pipeline.invalidate_cache()
    results, _ = await comp.search_pipeline.search("retainedarchiveword", top_k=5)
    return len(results)


# --- release_absence_hold -------------------------------------------------


@pytest.mark.parametrize("reason", ["source_missing", "scan_missing"])
async def test_an_absence_hold_and_its_pending_row_are_lifted(env, reason):
    comp, mem_dir = env
    path = await _indexed(comp, mem_dir / "a.md")
    await comp.storage.hold_source(path, reason)
    assert comp.storage.queue_source_check_sync(path) is not None
    before = await comp.storage.source_hold_generation(path)

    assert await comp.storage.release_absence_hold(norm_path(path)) is True

    assert not await _hidden(comp, path)
    assert not await comp.storage.is_source_pending(path)
    assert await comp.storage.source_hold_generation(path) > before


@pytest.mark.parametrize(
    "reason",
    [
        "watch_reindex_unresolved",
        "scan_unavailable",
        REDACTION_BLOCKED_HOLD,
        "stat_error:5",
        "watch_root_removed",
    ],
)
async def test_any_other_hold_stays_with_its_pending_row(env, reason):
    comp, mem_dir = env
    path = await _indexed(comp, mem_dir / "a.md")
    await comp.storage.hold_source(path, reason)
    comp.storage.queue_source_check_sync(path)
    before = await comp.storage.source_hold_generation(path)

    assert await comp.storage.release_absence_hold(norm_path(path)) is False

    assert await comp.storage.is_source_held(Path(norm_path(path)))
    assert await comp.storage.is_source_pending(path)
    assert await comp.storage.source_hold_generation(path) == before


async def test_a_hold_that_became_unavailable_after_the_scan_stays(env):
    """The scan saw it missing; by the release the mount is unavailable."""
    comp, mem_dir = env
    path = await _indexed(comp, mem_dir / "a.md")
    await comp.storage.hold_source(path, "scan_missing")
    await comp.storage.hold_source(path, "scan_unavailable")

    assert await comp.storage.release_absence_hold(norm_path(path)) is False
    assert await _hidden(comp, path)


async def test_a_pending_only_source_is_shown_and_an_unknown_key_changes_nothing(env):
    comp, mem_dir = env
    path = await _indexed(comp, mem_dir / "a.md")
    comp.storage.queue_source_check_sync(path)
    assert await _hidden(comp, path)

    assert await comp.storage.release_absence_hold(norm_path(path)) is True
    assert not await _hidden(comp, path)
    assert await comp.storage.release_absence_hold(norm_path(path)) is False


# --- RetainRule -------------------------------------------------------------


def test_rule_needs_a_retain_dir_and_an_index_root(tmp_path, monkeypatch):
    # ``expanduser`` reads USERPROFILE on Windows, HOME elsewhere.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    root = tmp_path / "root"
    plans = root / "plans"
    plans.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    rule = RetainRule.prepare(
        IndexingConfig(memory_dirs=[root], retain_missing_dirs=["~/root/plans", outside])
    )

    assert rule.retains(plans / "a.md")
    assert rule.retains(plans / "deep" / "b.md")
    assert not rule.retains(root / "c.md")
    assert not rule.retains(root / "plans-old" / "d.md")  # a sibling, not a child
    assert not rule.retains(outside / "e.md")  # retained, but under no index root


def test_an_empty_rule_does_no_filesystem_work(monkeypatch, tmp_path):
    def boom(*_a, **_k):
        raise AssertionError("resolved a path for an empty retain list")

    monkeypatch.setattr(orphan_detect, "norm_dir_prefix", boom)
    monkeypatch.setattr(orphan_detect, "norm_path", boom)
    rule = RetainRule.prepare(IndexingConfig(memory_dirs=[tmp_path]))
    assert rule == RetainRule()
    assert not rule.retains(tmp_path / "a.md")
    assert rule.split([tmp_path / "a.md"]) == ([], [tmp_path / "a.md"])


# --- engine -------------------------------------------------------------------


async def test_a_deleted_file_under_a_retain_dir_stays_searchable(env):
    comp, mem_dir = env
    path = await _indexed(comp, mem_dir / "a.md")
    path.unlink()
    comp.storage.queue_source_check_sync(path)  # the watcher's delete journal

    await comp.index_engine.index_file(path)

    assert not await _hidden(comp, path)
    assert not await comp.storage.is_source_pending(path)
    assert await _hits(comp) == 1


async def test_a_deleted_file_outside_every_retain_dir_is_held(env):
    comp, mem_dir = env
    comp.config.indexing.retain_missing_dirs = [mem_dir / "elsewhere"]
    path = await _indexed(comp, mem_dir / "a.md")
    path.unlink()

    await comp.index_engine.index_file(path)

    assert await comp.storage.is_source_held(Path(norm_path(path)))
    assert await _hits(comp) == 0


async def test_a_redaction_hold_under_a_retain_dir_stays(env):
    comp, mem_dir = env
    path = await _indexed(comp, mem_dir / "a.md")
    await comp.storage.hold_source(path, REDACTION_BLOCKED_HOLD)
    path.unlink()

    await comp.index_engine.index_file(path)

    assert await comp.storage.is_source_held(Path(norm_path(path)))


async def test_a_path_that_now_holds_a_directory_is_not_an_archive(env):
    """Unchanged from before #2833: the stat branch deletes a non-file."""
    comp, mem_dir = env
    path = await _indexed(comp, mem_dir / "a.md")
    path.unlink()
    path.mkdir()

    await comp.index_engine.index_file(path)

    assert await comp.storage.list_chunks_by_source(path) == []


# --- watcher recheck ----------------------------------------------------------


async def _run_recheck(watcher, seconds: float) -> None:
    task = asyncio.create_task(watcher._recheck_held_loop())
    await asyncio.sleep(seconds)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_recheck_lifts_a_hold_placed_before_the_dir_was_retained(env):
    comp, mem_dir = env
    path = await _indexed(comp, mem_dir / "a.md")
    path.unlink()
    await comp.storage.hold_source(path, "scan_missing")
    watcher = FileWatcher(comp.index_engine, comp.config.indexing)

    for _ in range(50):
        await _run_recheck(watcher, 0.05)
        if not await _hidden(comp, path):
            break
    assert not await _hidden(comp, path)
    assert await _hits(comp) == 1


async def test_recheck_does_not_spin_on_a_refused_release(env, monkeypatch):
    comp, mem_dir = env
    path = await _indexed(comp, mem_dir / "a.md")
    path.unlink()
    await comp.storage.hold_source(path, REDACTION_BLOCKED_HOLD)
    release = AsyncMock(wraps=comp.storage.release_absence_hold)
    monkeypatch.setattr(comp.storage, "release_absence_hold", release)
    watcher = FileWatcher(comp.index_engine, comp.config.indexing)

    await _run_recheck(watcher, 0.3)

    assert release.await_count == 1
    assert await comp.storage.is_source_held(Path(norm_path(path)))


# --- automatic cleanup and health ---------------------------------------------


async def test_compaction_shows_retained_sources_and_holds_the_rest(env, tmp_path):
    from memtomem.scheduler.jobs import _run_compaction

    comp, mem_dir = env
    kept = await _indexed(comp, mem_dir / "plans" / "a.md")
    other_root = tmp_path / "other"
    comp.config.indexing.memory_dirs = [mem_dir, other_root]
    comp.config.indexing.retain_missing_dirs = [mem_dir / "plans"]
    gone = await _indexed(comp, other_root / "b.md")
    await comp.storage.hold_source(kept, "scan_missing")
    kept.unlink()
    gone.unlink()

    result = await _run_compaction(AppContext.from_components(comp))

    assert (result["archived_sources"], result["orphan_files"], result["held_sources"]) == (
        1,
        1,
        1,
    )
    assert not await _hidden(comp, kept)
    assert await comp.storage.is_source_held(Path(norm_path(gone)))


async def test_health_does_not_count_a_retained_source_as_orphaned(env):
    comp, mem_dir = env
    path = await _indexed(comp, mem_dir / "a.md")
    path.unlink()
    app = AppContext.from_components(comp)

    assert (await check_orphan_count(app)).value["orphaned"] == 0
    comp.config.indexing.retain_missing_dirs = []
    assert (await check_orphan_count(app)).value["orphaned"] == 1


# --- explicit purge -------------------------------------------------------------


async def test_purge_neither_lists_nor_deletes_a_retained_source(env, monkeypatch):
    comp, mem_dir = env
    path = await _indexed(comp, mem_dir / "a.md")
    path.unlink()
    app = AppContext.from_components(comp)

    async def fake_app(_ctx):
        return app

    monkeypatch.setattr(dedup_decay, "_get_app_initialized", fake_app)
    monkeypatch.setattr(
        dedup_decay, "capture_session_for_untracked_write", AsyncMock(return_value=None)
    )

    out = await dedup_decay.mem_cleanup_orphans(dry_run=False, confirm_purge=True, ctx=None)

    assert "1 archived source(s) under retain_missing_dirs are kept." in out
    assert str(path.name) not in out
    assert len(await comp.storage.list_chunks_by_source(path)) == 1


# --- edits --------------------------------------------------------------------


async def test_editing_an_archived_chunk_is_refused(env):
    from test_excluded_source_mutation import _request

    comp, mem_dir = env
    path = await _indexed(comp, mem_dir / "a.md")
    (chunk,) = await comp.storage.list_chunks_by_source(path)
    path.unlink()
    await comp.index_engine.index_file(path)

    assert await memory_crud.mem_edit(
        str(chunk.id), "NEW", ctx=StubCtx(AppContext.from_components(comp))
    ) == (f"Error: {SOURCE_MISSING_DETAIL}")
    response = await _request(comp, "web_edit", chunk.id)
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == SOURCE_MISSING_DETAIL
    assert not path.exists()


# --- reports ------------------------------------------------------------------


async def test_status_reports_an_archive_instead_of_an_orphan(env):
    from memtomem.server.tools.status_config import collect_status_report, iter_status_lines

    comp, mem_dir = env
    path = await _indexed(comp, mem_dir / "a.md")
    path.unlink()

    report = await collect_status_report(AppContext.from_components(comp))

    assert (report["index"]["orphaned_sources"], report["index"]["archived_sources"]) == (0, 1)
    assert report["config"]["retain_missing_dirs"] == [str(mem_dir.resolve())]
    archived = [line for line in iter_status_lines(report) if line.key.startswith("Archived:")]
    assert [line.value for line in archived] == ["1"]


# --- review round 1 regressions ------------------------------------------------


@pytest.mark.requires_symlinks
async def test_purge_keeps_an_archive_a_relinked_source_would_reach(env, monkeypatch):
    """``delete_by_source`` re-resolves (#2835): judge the deletion identity too."""
    from contextlib import asynccontextmanager

    from memtomem.cli import _bootstrap, gc_cmd

    comp, mem_dir = env
    plans = mem_dir / "plans"
    comp.config.indexing.retain_missing_dirs = [plans]
    archive = await _indexed(comp, plans / "archive.md")
    old = await _indexed(comp, mem_dir / "notes" / "old.md")
    archive.unlink()
    old.unlink()
    old.symlink_to(archive)  # dangling: both sources stat as missing
    app = AppContext.from_components(comp)

    async def fake_app(_ctx):
        return app

    @asynccontextmanager
    async def components():
        yield comp

    monkeypatch.setattr(dedup_decay, "_get_app_initialized", fake_app)
    monkeypatch.setattr(
        dedup_decay, "capture_session_for_untracked_write", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(_bootstrap, "cli_components", components)

    out = await dedup_decay.mem_cleanup_orphans(dry_run=False, confirm_purge=True, ctx=None)
    await gc_cmd._run_orphan_sources(apply_=True, assume_yes=True)

    assert "2 archived source(s) under retain_missing_dirs are kept." in out
    assert len(await comp.storage.list_chunks_by_source(archive)) == 1


async def test_a_confirmed_purge_also_reports_what_it_kept(env, tmp_path, monkeypatch):
    comp, mem_dir = env
    other = tmp_path / "other"
    comp.config.indexing.memory_dirs = [mem_dir, other]
    kept = await _indexed(comp, mem_dir / "a.md")
    gone = await _indexed(comp, other / "b.md")
    kept.unlink()
    gone.unlink()
    app = AppContext.from_components(comp)

    async def fake_app(_ctx):
        return app

    monkeypatch.setattr(dedup_decay, "_get_app_initialized", fake_app)
    monkeypatch.setattr(
        dedup_decay, "capture_session_for_untracked_write", AsyncMock(return_value=None)
    )

    out = await dedup_decay.mem_cleanup_orphans(dry_run=False, confirm_purge=True, ctx=None)

    assert out.startswith("Cleanup complete:")
    assert out.endswith("1 archived source(s) under retain_missing_dirs are kept.")
    assert await comp.storage.list_chunks_by_source(gone) == []


async def test_the_engine_judges_and_releases_one_normalized_key(env, monkeypatch):
    from memtomem.indexing import engine as engine_mod

    comp, mem_dir = env
    calls: list[Path] = []
    real = engine_mod.norm_path

    def counting(path):
        calls.append(path)
        return real(path)

    monkeypatch.setattr(engine_mod, "norm_path", counting)
    key = comp.index_engine._retained_key(mem_dir / "a.md")

    assert key == real(mem_dir / "a.md")
    assert calls == [mem_dir / "a.md"]


async def test_a_read_that_finds_a_directory_is_held_not_archived(env, monkeypatch):
    """The read-race branch: the stat saw a file, the read found a directory."""
    comp, mem_dir = env
    path = await _indexed(comp, mem_dir / "a.md")
    real_read_text = Path.read_text

    def read_text(self, *args, **kwargs):
        if self == path:
            raise IsADirectoryError(21, "Is a directory", str(self))
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    path.write_text(BODY + "changed\n", encoding="utf-8")  # defeat the receipt hit

    await comp.index_engine.index_file(path)

    assert await comp.storage.is_source_held(Path(norm_path(path)))


@pytest.mark.requires_symlinks
async def test_a_looping_retain_dir_neither_stops_compaction_nor_status(env, tmp_path):
    """Python 3.12 raises ``RuntimeError`` resolving a symlink loop."""
    from memtomem.scheduler.jobs import _run_compaction
    from memtomem.server.tools.status_config import collect_status_report

    comp, mem_dir = env
    loop = tmp_path / "loop"
    loop.symlink_to(loop)
    comp.config.indexing.retain_missing_dirs = [loop, mem_dir / "plans"]
    kept = await _indexed(comp, mem_dir / "plans" / "a.md")
    gone = await _indexed(comp, mem_dir / "b.md")
    await comp.storage.hold_source(kept, "scan_missing")
    kept.unlink()
    gone.unlink()
    app = AppContext.from_components(comp)

    result = await _run_compaction(app)
    report = await collect_status_report(app)

    assert (result["archived_sources"], result["held_sources"]) == (1, 1)
    assert not await _hidden(comp, kept)
    assert await comp.storage.is_source_held(Path(norm_path(gone)))
    assert report["config"]["retain_missing_dirs"][0] == str(loop)


# --- commit review regressions --------------------------------------------------


@pytest.mark.requires_symlinks
async def test_an_unavailable_source_under_a_retain_dir_is_not_an_archive(env):
    """``exists()`` is False for a symlink loop too; only absence is archived."""
    from memtomem.server.tools.status_config import collect_status_report

    comp, mem_dir = env
    path = await _indexed(comp, mem_dir / "a.md")
    path.unlink()
    path.symlink_to(path)  # ELOOP: unavailable, not missing
    app = AppContext.from_components(comp)

    assert (await check_orphan_count(app)).value["orphaned"] == 1
    report = await collect_status_report(app)
    assert (report["index"]["orphaned_sources"], report["index"]["archived_sources"]) == (1, 0)


@pytest.mark.parametrize("surface", ["mcp_edit", "web_edit", "mcp_delete"])
async def test_a_file_removed_after_the_gates_is_refused_at_the_read(env, monkeypatch, surface):
    """The absence is decided where the pre-image is read, not by an earlier check."""
    from memtomem.tools import memory_mutation
    from test_excluded_source_mutation import _request

    comp, mem_dir = env
    path = await _indexed(comp, mem_dir / "a.md")
    (chunk,) = await comp.storage.list_chunks_by_source(path)

    def gone(_source):
        raise FileNotFoundError(2, "No such file or directory", str(path))

    monkeypatch.setattr(memory_crud, "read_pre_image", gone)
    monkeypatch.setattr(memory_mutation, "read_pre_image", gone)

    result = await _request(comp, surface, chunk.id)

    if isinstance(result, str):
        assert result == f"Error: {SOURCE_MISSING_DETAIL}"
    else:
        assert result.status_code == 409, result.text
        assert result.json()["detail"] == SOURCE_MISSING_DETAIL
    assert path.read_text(encoding="utf-8") == BODY
