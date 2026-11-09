"""An exclusion installed between an import's write and its index removes the old chunks (#3158).

The importers write every target first and index them afterwards. When
``indexing.exclude_patterns`` changes in between (``mem_config`` can do that at
runtime), the engine skips the freshly written file. Without a purge the chunks
of the previous import stay searchable beside bytes that are no longer on disk.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from helpers import StubCtx
from memtomem.server.context import AppContext
from memtomem.storage.sqlite_helpers import norm_path

KINDS = ["obsidian", "notion"]


def _vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "kept.md").write_text("# Kept\n\nkept body\n", encoding="utf-8")
    (vault / "hidden.md").write_text("# Hidden\n\noldword body\n", encoding="utf-8")
    return vault


def _exclude_after_write(monkeypatch, comp, kind: str, pattern: str) -> None:
    """Install ``pattern`` once the importer has written every target, before indexing."""
    import memtomem.indexing.importers as importers_mod

    original = getattr(importers_mod, f"import_{kind}")

    async def wrapped(*args, **kwargs):
        written = await original(*args, **kwargs)
        comp.config.indexing.exclude_patterns = [
            *comp.config.indexing.exclude_patterns,
            pattern,
        ]
        return written

    monkeypatch.setattr(importers_mod, f"import_{kind}", wrapped)


async def _chunk_texts(comp, path: Path) -> list[str]:
    return [c.content for c in await comp.storage.list_chunks_by_source(path)]


@pytest.mark.parametrize("kind", KINDS)
async def test_reimport_removes_old_chunks_of_a_target_excluded_after_its_write(
    bm25_only_components, tmp_path, monkeypatch, kind
):
    from memtomem.server.tools import importers

    comp, mem_dir = bm25_only_components
    ctx = StubCtx(AppContext.from_components(comp))
    tool = getattr(importers, f"mem_import_{kind}")
    vault = _vault(tmp_path)
    out = mem_dir / "_imported" / kind
    await tool(str(vault), ctx=ctx)
    assert any("oldword" in t for t in await _chunk_texts(comp, out / "hidden.md"))
    assert await comp.storage.bm25_search("oldword", 10)

    (vault / "hidden.md").write_text("# Hidden\n\nnewword body\n", encoding="utf-8")
    _exclude_after_write(monkeypatch, comp, kind, f"**/_imported/{kind}/hidden.md")
    result = await tool(str(vault), ctx=ctx)

    assert "newword" in (out / "hidden.md").read_text(encoding="utf-8")
    # What the issue is about: the overwritten text is no longer found by search.
    assert not await comp.storage.bm25_search("oldword", 10)
    assert await _chunk_texts(comp, out / "hidden.md") == []
    assert any("kept" in t for t in await _chunk_texts(comp, out / "kept.md"))
    lines = result.splitlines()
    assert "- Files imported: 2" in lines
    removed = [ln for ln in lines if ln.startswith("- Written but excluded from indexing:")]
    assert removed and removed[0].startswith("- Written but excluded from indexing: 1 (")
    assert int(removed[0].rsplit(": ", 1)[1].rstrip(")")) > 0


@pytest.mark.parametrize("kind", KINDS)
async def test_reimport_that_only_purges_still_flags_the_session(
    bm25_only_components, tmp_path, monkeypatch, kind
):
    from memtomem.server.tools import importers

    comp, mem_dir = bm25_only_components
    ctx = StubCtx(AppContext.from_components(comp))
    tool = getattr(importers, f"mem_import_{kind}")
    vault = _vault(tmp_path)
    await tool(str(vault), ctx=ctx)

    flag = AsyncMock()
    monkeypatch.setattr(importers, "flag_untracked_write", flag)
    _exclude_after_write(monkeypatch, comp, kind, f"**/_imported/{kind}/**")
    result = await tool(str(vault), ctx=ctx)

    assert "- Chunks indexed: 0" in result.splitlines()
    assert not await comp.storage.get_all_source_files()
    flag.assert_awaited_once()


async def _indexed(comp, path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    stats = await comp.index_engine.index_file(path)
    assert stats.indexed_chunks > 0


def _exclude(comp, pattern: str) -> None:
    comp.config.indexing.exclude_patterns = [*comp.config.indexing.exclude_patterns, pattern]


async def test_default_index_of_an_excluded_file_keeps_its_chunks(bm25_only_components):
    comp, mem_dir = bm25_only_components
    path = mem_dir / "note.md"
    await _indexed(comp, path, "# Note\n\nalpha body\n")
    _exclude(comp, "**/note.md")

    stats = await comp.index_engine.index_file(path)

    assert stats.excluded_files == 1
    assert stats.deleted_chunks == 0
    assert await _chunk_texts(comp, path) != []


def _visibility_rows(comp, path: Path) -> tuple[int, int]:
    db = comp.storage._get_db()
    key = norm_path(path)
    held = db.execute("SELECT COUNT(*) FROM held_sources WHERE source_file=?", (key,))
    pending = db.execute("SELECT COUNT(*) FROM pending_source_checks WHERE source_file=?", (key,))
    return held.fetchone()[0], pending.fetchone()[0]


async def test_purge_if_excluded_deletes_the_chunks_and_clears_hold_and_pending(
    bm25_only_components,
):
    comp, mem_dir = bm25_only_components
    path = mem_dir / "note.md"
    await _indexed(comp, path, "# Note\n\nalpha body\n")
    # Hold first: ``hold_source`` itself clears a pending check.
    await comp.storage.hold_source(path, "source_missing")
    comp.storage.queue_source_check_sync(path)
    assert _visibility_rows(comp, path) == (1, 1)
    _exclude(comp, "**/note.md")

    stats = await comp.index_engine.index_file(path, purge_if_excluded=True)

    assert stats.excluded_files == 1
    assert stats.deleted_chunks > 0
    assert stats.mutated is True
    assert await _chunk_texts(comp, path) == []
    # Read the tables directly: the storage getters skip sources with no chunks.
    assert _visibility_rows(comp, path) == (0, 0)


async def test_purge_if_excluded_indexes_a_file_that_is_not_excluded(bm25_only_components):
    comp, mem_dir = bm25_only_components
    path = mem_dir / "note.md"
    path.write_text("# Note\n\nalpha body\n", encoding="utf-8")

    stats = await comp.index_engine.index_file(path, purge_if_excluded=True)

    assert stats.excluded_files == 0
    assert stats.indexed_chunks > 0


def _link_or_skip(link: Path, target: Path) -> None:
    link.unlink()
    try:
        link.symlink_to(target)
    except OSError as exc:  # Windows without symlink privilege
        pytest.skip(f"cannot create symlink: {exc}")


async def _stored_keys(comp) -> set[str]:
    return {str(p) for p in await comp.storage.get_all_source_files()}


async def test_purge_if_excluded_deletes_a_symlinked_path_own_rows_not_its_target(
    bm25_only_components,
):
    comp, mem_dir = bm25_only_components
    written = mem_dir / "written.md"
    target = mem_dir / "target.md"
    await _indexed(comp, written, "# Written\n\nwritten body\n")
    await _indexed(comp, target, "# Target\n\ntarget body\n")
    written_key, target_key = norm_path(written), norm_path(target)
    _link_or_skip(written, target)
    _exclude(comp, "**/target.md")
    _exclude(comp, "**/written.md")

    stats = await comp.index_engine.index_file(written, purge_if_excluded=True)

    assert stats.excluded_files == 1
    keys = await _stored_keys(comp)
    assert target_key in keys
    assert written_key not in keys


async def test_a_leaf_relinked_while_waiting_for_the_lock_keeps_the_target_rows(
    bm25_only_components, tmp_path
):
    comp, mem_dir = bm25_only_components
    engine = comp.index_engine
    probe = tmp_path / "probe"
    probe.write_text("", encoding="utf-8")
    _link_or_skip(probe, tmp_path)  # skip before any task exists
    written = mem_dir / "written.md"
    target = mem_dir / "target.md"
    await _indexed(comp, written, "# Written\n\nwritten body\n")
    await _indexed(comp, target, "# Target\n\ntarget body\n")
    written_key, target_key = norm_path(written), norm_path(target)
    # The guard judges the path it stats, which follows the new link, so the
    # delete that could go wrong is the one where the link's target is excluded.
    _exclude(comp, "**/target.md")
    _exclude(comp, "**/written.md")

    async with engine._index_lock:
        task = asyncio.create_task(engine.index_file(written, purge_if_excluded=True))
        for _ in range(50):
            await asyncio.sleep(0)
        assert not task.done()
        _link_or_skip(written, target)
    stats = await asyncio.wait_for(task, timeout=10)

    assert stats.excluded_files == 1
    keys = await _stored_keys(comp)
    assert target_key in keys
    assert written_key not in keys


async def test_the_purge_key_never_resolves_the_leaf(bm25_only_components, monkeypatch):
    """A leaf swapped for a link between any check and the key capture is not followed."""
    from memtomem.storage import sqlite_helpers

    comp, mem_dir = bm25_only_components
    written = mem_dir / "written.md"
    target = mem_dir / "target.md"
    await _indexed(comp, written, "# Written\n\nwritten body\n")
    await _indexed(comp, target, "# Target\n\ntarget body\n")
    written_key, target_key = norm_path(written), norm_path(target)
    _link_or_skip(written, target)
    _exclude(comp, "**/target.md")
    _exclude(comp, "**/written.md")
    # Hide the link from every ``is_symlink`` check, as if it were made right after.
    monkeypatch.setattr(Path, "is_symlink", lambda self: False)
    assert sqlite_helpers.norm_path(written) == target_key  # resolving would follow it

    await comp.index_engine.index_file(written, purge_if_excluded=True)

    keys = await _stored_keys(comp)
    assert target_key in keys
    assert written_key not in keys


@pytest.mark.skipif(sys.platform != "win32", reason="only Windows resolve() changes the case")
async def test_a_leaf_spelled_in_another_case_purges_nothing_on_windows(bm25_only_components):
    """The cost of never following the leaf: like ``stored_key_for``, no case matching."""
    comp, mem_dir = bm25_only_components
    stored = mem_dir / "Note.md"
    await _indexed(comp, stored, "# Note\n\nalpha body\n")
    _exclude(comp, "**/[Nn]ote.md")

    stats = await comp.index_engine.index_file(mem_dir / "note.md", purge_if_excluded=True)

    assert stats.excluded_files == 1
    assert stats.deleted_chunks == 0
    assert norm_path(stored) in await _stored_keys(comp)


async def test_bulk_index_counts_a_file_excluded_after_discovery(bm25_only_components):
    comp, mem_dir = bm25_only_components
    engine = comp.index_engine
    (mem_dir / "a.md").write_text("# A\n\nalpha body\n", encoding="utf-8")
    (mem_dir / "b.md").write_text("# B\n\nbeta body\n", encoding="utf-8")
    original = engine._discover_files

    def discover_then_exclude(*args, **kwargs):
        files = original(*args, **kwargs)
        _exclude(comp, "**/b.md")
        return files

    engine._discover_files = discover_then_exclude
    try:
        stats = await engine.index_path(mem_dir)
    finally:
        del engine._discover_files

    assert stats.excluded_files == 1
    assert await _chunk_texts(comp, mem_dir / "b.md") == []
