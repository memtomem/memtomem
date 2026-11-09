"""An import target replaced by a symlink between its write and its index is held (#3164).

The importers write every target first and index them afterwards. When a
target is swapped for a symlink in between, ``index_file`` would resolve the
link, index the link's target as the import's file (its namespace and tags),
and leave the previous import's chunks under the target path's own key
searchable. ``hold_if_relinked`` holds that key with ``source_relinked`` and
indexes nothing through the link.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import memtomem.indexing.engine as engine_mod
from helpers import StubCtx
from memtomem.server.context import AppContext
from memtomem.storage.source_holds import RELINKED_HOLD
from memtomem.storage.sqlite_helpers import norm_path

KINDS = ["obsidian", "notion"]
REPORT_LINE = "- Written but replaced by a symlink (not indexed, previous chunks hidden): "


def _link_or_skip(link: Path, target: Path | str) -> None:
    link.unlink(missing_ok=True)
    try:
        link.symlink_to(target)
    except OSError as exc:  # Windows without symlink privilege
        pytest.skip(f"cannot create symlink: {exc}")


def _probe_symlinks(tmp_path: Path) -> None:
    """Skip before any task exists when this platform cannot make a symlink."""
    probe = tmp_path / "probe"
    probe.write_text("", encoding="utf-8")
    _link_or_skip(probe, tmp_path)
    probe.unlink()


def _reason(comp, key: str) -> str | None:
    row = (
        comp.storage._get_db()
        .execute("SELECT reason FROM held_sources WHERE source_file=?", (key,))
        .fetchone()
    )
    return row[0] if row else None


async def _rows(comp, key: str) -> list[tuple[str, str]]:
    """``(content, namespace)`` of the rows stored under exactly ``key``.

    Read by the key itself: ``list_chunks_by_source`` resolves its argument,
    which for a relinked leaf reads the link target's rows.
    """
    return (
        comp.storage._get_db()
        .execute("SELECT content, namespace FROM chunks WHERE source_file=?", (key,))
        .fetchall()
    )


async def _hits(comp, query: str) -> list[str]:
    results, _ = await comp.search_pipeline.search(query, top_k=10)
    return [r.chunk.content for r in results]


async def _indexed(comp, path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    stats = await comp.index_engine.index_file(path)
    assert stats.indexed_chunks > 0


def _exclude(comp, pattern: str) -> None:
    comp.config.indexing.exclude_patterns = [*comp.config.indexing.exclude_patterns, pattern]


# --- the MCP handlers -------------------------------------------------------


def _relink_after_write(monkeypatch, kind: str, leaf: Path, target: Path) -> None:
    """Replace ``leaf`` with a link to ``target`` once the importer has written it."""
    import memtomem.indexing.importers as importers_mod

    original = getattr(importers_mod, f"import_{kind}")

    async def wrapped(*args, **kwargs):
        written = await original(*args, **kwargs)
        _link_or_skip(leaf, target)
        return written

    monkeypatch.setattr(importers_mod, f"import_{kind}", wrapped)


async def _imported_once(comp, tmp_path: Path, mem_dir: Path, kind: str):
    from memtomem.server.tools import importers

    ctx = StubCtx(AppContext.from_components(comp))
    tool = getattr(importers, f"mem_import_{kind}")
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "hidden.md").write_text("# Hidden\n\noldword body\n", encoding="utf-8")
    await tool(str(vault), ctx=ctx)
    leaf = mem_dir / "_imported" / kind / "hidden.md"
    assert any("oldword" in c for c, _ in await _rows(comp, norm_path(leaf)))
    (vault / "hidden.md").write_text("# Hidden\n\nnewword body\n", encoding="utf-8")
    other = mem_dir / "other.md"
    other.write_text("# Other\n\notherword body\n", encoding="utf-8")
    return tool, ctx, vault, leaf, other


@pytest.mark.parametrize("kind", KINDS)
async def test_reimport_holds_a_target_replaced_by_a_symlink_and_indexes_nothing_through_it(
    bm25_only_components, tmp_path, monkeypatch, kind
):
    comp, mem_dir = bm25_only_components
    _probe_symlinks(tmp_path)
    tool, ctx, vault, leaf, other = await _imported_once(comp, tmp_path, mem_dir, kind)
    leaf_key = norm_path(leaf)
    _relink_after_write(monkeypatch, kind, leaf, other)

    result = await tool(str(vault), ctx=ctx, tags=["mine"])

    assert _reason(comp, leaf_key) == RELINKED_HOLD
    assert not any("oldword" in t for t in await _hits(comp, "oldword"))
    # The link's target was never indexed as the import's file.
    assert await _rows(comp, norm_path(other)) == []
    lines = result.splitlines()
    assert "- Chunks indexed: 0" in lines
    assert f"{REPORT_LINE}1" in lines


@pytest.mark.parametrize("kind", KINDS)
async def test_the_tag_pass_leaves_an_indexed_link_target_alone(
    bm25_only_components, tmp_path, monkeypatch, kind
):
    """A mixed import runs the tag pass; the relinked file's target is not in it."""
    comp, mem_dir = bm25_only_components
    _probe_symlinks(tmp_path)
    tool, ctx, vault, leaf, other = await _imported_once(comp, tmp_path, mem_dir, kind)
    (vault / "kept.md").write_text("# Kept\n\nkeptword body\n", encoding="utf-8")
    await comp.index_engine.index_file(other)
    before = {c.metadata.tags for c in await comp.storage.list_chunks_by_source(other)}
    _relink_after_write(monkeypatch, kind, leaf, other)

    result = await tool(str(vault), ctx=ctx, tags=["mine"])

    kept = mem_dir / "_imported" / kind / "kept.md"
    assert all("mine" in c.metadata.tags for c in await comp.storage.list_chunks_by_source(kept))
    after = {c.metadata.tags for c in await comp.storage.list_chunks_by_source(other)}
    assert after == before
    assert f"{REPORT_LINE}1" in result.splitlines()


@pytest.mark.parametrize("kind", KINDS)
async def test_a_later_clean_reimport_lifts_the_hold(
    bm25_only_components, tmp_path, monkeypatch, kind
):
    comp, mem_dir = bm25_only_components
    _probe_symlinks(tmp_path)
    tool, ctx, vault, leaf, other = await _imported_once(comp, tmp_path, mem_dir, kind)
    _relink_after_write(monkeypatch, kind, leaf, other)
    await tool(str(vault), ctx=ctx)
    monkeypatch.undo()
    leaf.unlink()

    result = await tool(str(vault), ctx=ctx)

    assert _reason(comp, norm_path(leaf)) is None
    assert any("newword" in t for t in await _hits(comp, "newword"))
    assert f"{REPORT_LINE}0" in result.splitlines()


@pytest.mark.parametrize("kind", KINDS)
async def test_a_reimport_that_only_holds_still_flags_the_session(
    bm25_only_components, tmp_path, monkeypatch, kind
):
    from memtomem.server.tools import importers

    comp, mem_dir = bm25_only_components
    _probe_symlinks(tmp_path)
    tool, ctx, vault, leaf, other = await _imported_once(comp, tmp_path, mem_dir, kind)
    flag = AsyncMock()
    monkeypatch.setattr(importers, "flag_untracked_write", flag)
    _relink_after_write(monkeypatch, kind, leaf, other)

    result = await tool(str(vault), ctx=ctx)

    assert "- Chunks indexed: 0" in result.splitlines()
    flag.assert_awaited_once()


# --- the engine -------------------------------------------------------------


async def _written_and_target(comp, mem_dir: Path) -> tuple[Path, Path, str, str]:
    written = mem_dir / "written.md"
    target = mem_dir / "target.md"
    await _indexed(comp, written, "# Written\n\nwrittenword body\n")
    await _indexed(comp, target, "# Target\n\ntargetword body\n")
    return written, target, norm_path(written), norm_path(target)


async def test_default_index_of_a_relinked_leaf_still_indexes_its_target(
    bm25_only_components, tmp_path
):
    """Every caller but the importers keeps indexing through the link."""
    comp, mem_dir = bm25_only_components
    _probe_symlinks(tmp_path)
    written, target, written_key, _ = await _written_and_target(comp, mem_dir)
    _link_or_skip(written, target)

    stats = await comp.index_engine.index_file(written)

    assert stats.relinked_files == 0
    assert stats.total_files == 1
    assert _reason(comp, written_key) is None


async def test_hold_if_relinked_holds_the_leaf_and_leaves_the_target_alone(
    bm25_only_components, tmp_path
):
    comp, mem_dir = bm25_only_components
    _probe_symlinks(tmp_path)
    written, target, written_key, target_key = await _written_and_target(comp, mem_dir)
    _link_or_skip(written, target)

    stats = await comp.index_engine.index_file(written, namespace="ns", hold_if_relinked=True)

    assert stats.relinked_files == 1
    assert stats.mutated is True
    assert stats.indexed_chunks == 0
    assert _reason(comp, written_key) == RELINKED_HOLD
    assert _reason(comp, target_key) is None
    assert {ns for _, ns in await _rows(comp, target_key)} != {"ns"}


async def test_hold_if_relinked_holds_a_dangling_link(bm25_only_components, tmp_path):
    comp, mem_dir = bm25_only_components
    _probe_symlinks(tmp_path)
    written, _, written_key, _ = await _written_and_target(comp, mem_dir)
    _link_or_skip(written, mem_dir / "missing.md")

    stats = await comp.index_engine.index_file(written, hold_if_relinked=True)

    assert stats.relinked_files == 1
    assert stats.deleted_chunks == 0
    assert await _rows(comp, written_key) != []
    assert _reason(comp, written_key) == RELINKED_HOLD


@pytest.mark.parametrize("purge", [True, False], ids=["with-purge", "hold-only"])
async def test_a_relinked_and_excluded_leaf_is_held_not_purged(
    bm25_only_components, tmp_path, purge
):
    comp, mem_dir = bm25_only_components
    _probe_symlinks(tmp_path)
    written, target, written_key, target_key = await _written_and_target(comp, mem_dir)
    _link_or_skip(written, target)
    _exclude(comp, "**/written.md")
    _exclude(comp, "**/target.md")

    stats = await comp.index_engine.index_file(
        written, purge_if_excluded=purge, hold_if_relinked=True
    )

    assert stats.relinked_files == 1
    assert stats.excluded_files == 0
    assert await _rows(comp, written_key) != []
    assert await _rows(comp, target_key) != []
    assert _reason(comp, written_key) == RELINKED_HOLD


async def _while_holding_the_index_lock(engine, call, swap) -> object:
    async with engine._index_lock:
        task = asyncio.create_task(call())
        for _ in range(50):
            await asyncio.sleep(0)
        assert not task.done()
        swap()
    return await asyncio.wait_for(task, timeout=10)


async def test_a_leaf_relinked_while_waiting_for_the_lock_is_held(bm25_only_components, tmp_path):
    comp, mem_dir = bm25_only_components
    _probe_symlinks(tmp_path)
    written, target, written_key, target_key = await _written_and_target(comp, mem_dir)

    stats = await _while_holding_the_index_lock(
        comp.index_engine,
        lambda: comp.index_engine.index_file(written, namespace="ns", hold_if_relinked=True),
        lambda: _link_or_skip(written, target),
    )

    assert stats.relinked_files == 1
    assert _reason(comp, written_key) == RELINKED_HOLD
    assert _reason(comp, target_key) is None
    assert {ns for _, ns in await _rows(comp, target_key)} != {"ns"}


async def test_a_link_restored_to_a_file_while_waiting_never_indexes_the_old_target(
    bm25_only_components, tmp_path
):
    """The resolution taken before the locks named the target; the leaf no longer does."""
    comp, mem_dir = bm25_only_components
    _probe_symlinks(tmp_path)
    written, target, written_key, target_key = await _written_and_target(comp, mem_dir)
    _link_or_skip(written, target)
    original = engine_mod._leaf_relinked
    calls = []

    def first_check_passes(own_key, work_path):
        calls.append(work_path)
        # Let the check before the locks pass, as if the swap below came first.
        return False if len(calls) == 1 else original(own_key, work_path)

    def restore():
        written.unlink()
        written.write_text("# Written\n\nrestoredword body\n", encoding="utf-8")

    engine_mod._leaf_relinked = first_check_passes
    try:
        stats = await _while_holding_the_index_lock(
            comp.index_engine,
            lambda: comp.index_engine.index_file(written, namespace="ns", hold_if_relinked=True),
            restore,
        )
    finally:
        engine_mod._leaf_relinked = original

    assert len(calls) == 2
    assert stats.relinked_files == 1
    assert _reason(comp, written_key) == RELINKED_HOLD
    assert {ns for _, ns in await _rows(comp, target_key)} != {"ns"}


async def test_a_relinked_leaf_never_takes_its_target_sidecar(
    bm25_only_components, tmp_path, monkeypatch
):
    import memtomem.context._atomic as atomic

    comp, mem_dir = bm25_only_components
    _probe_symlinks(tmp_path)
    written, target, written_key, _ = await _written_and_target(comp, mem_dir)
    _link_or_skip(written, target)

    def refuse(*args, **kwargs):
        raise AssertionError("sidecar taken for a relinked leaf")

    monkeypatch.setattr(atomic, "async_file_lock", refuse)
    stats = await comp.index_engine.index_file(written, hold_if_relinked=True)

    assert stats.relinked_files == 1
    assert _reason(comp, written_key) == RELINKED_HOLD


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX directory permissions")
@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores modes")
async def test_a_leaf_linked_into_an_unwritable_directory_is_held(bm25_only_components, tmp_path):
    comp, mem_dir = bm25_only_components
    _probe_symlinks(tmp_path)
    written, _, written_key, _ = await _written_and_target(comp, mem_dir)
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "elsewhere.md").write_text("# Elsewhere\n\nelsewhere body\n", encoding="utf-8")
    _link_or_skip(written, locked / "elsewhere.md")
    locked.chmod(0o500)
    try:
        stats = await comp.index_engine.index_file(written, hold_if_relinked=True)
    finally:
        locked.chmod(0o700)

    assert stats.relinked_files == 1
    assert _reason(comp, written_key) == RELINKED_HOLD


async def test_hold_if_relinked_indexes_a_regular_file(bm25_only_components):
    comp, mem_dir = bm25_only_components
    path = mem_dir / "note.md"
    path.write_text("# Note\n\nalpha body\n", encoding="utf-8")

    stats = await comp.index_engine.index_file(path, hold_if_relinked=True)

    assert stats.relinked_files == 0
    assert stats.indexed_chunks > 0


async def test_a_leaf_relinked_before_its_sidecar_is_taken_raises_and_indexes_nothing(
    bm25_only_components, tmp_path, monkeypatch
):
    """A known limit: the sidecar is keyed after a pipeline slot is free.

    A leaf relinked in that wait, into a directory whose sidecar cannot be
    taken, fails the call with that error. It must not report the file as
    indexed, and nothing is written through the link.
    """
    import memtomem.context._atomic as atomic

    comp, mem_dir = bm25_only_components
    _probe_symlinks(tmp_path)
    written, target, written_key, target_key = await _written_and_target(comp, mem_dir)
    target_rows = await _rows(comp, target_key)
    real_key = atomic.memory_lock_path

    def relink_then_key(path):
        _link_or_skip(written, target)
        return real_key(path)

    def refuse(*args, **kwargs):
        raise PermissionError("target directory is not writable")

    monkeypatch.setattr(atomic, "memory_lock_path", relink_then_key)
    monkeypatch.setattr(atomic, "async_file_lock", refuse)
    with pytest.raises(PermissionError):
        await comp.index_engine.index_file(written, namespace="ns", hold_if_relinked=True)

    assert await _rows(comp, target_key) == target_rows


async def test_a_sidecar_failure_on_a_regular_file_still_raises(bm25_only_components, monkeypatch):
    import memtomem.context._atomic as atomic

    comp, mem_dir = bm25_only_components
    path = mem_dir / "note.md"
    path.write_text("# Note\n\nalpha body\n", encoding="utf-8")

    def refuse(*args, **kwargs):
        raise PermissionError("sidecar")

    monkeypatch.setattr(atomic, "async_file_lock", refuse)
    with pytest.raises(PermissionError):
        await comp.index_engine.index_file(path, hold_if_relinked=True)


async def test_the_hold_waits_for_another_task_write_transaction(bm25_only_components, tmp_path):
    comp, mem_dir = bm25_only_components
    _probe_symlinks(tmp_path)
    written, target, written_key, _ = await _written_and_target(comp, mem_dir)
    _link_or_skip(written, target)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def writer():
        async with comp.storage.transaction():
            entered.set()
            await release.wait()

    owner = asyncio.create_task(writer())
    await entered.wait()
    call = asyncio.create_task(comp.index_engine.index_file(written, hold_if_relinked=True))
    for _ in range(20):
        await asyncio.sleep(0)
    assert not call.done()
    release.set()
    stats = await asyncio.wait_for(call, timeout=10)
    await owner

    assert stats.relinked_files == 1
    assert _reason(comp, written_key) == RELINKED_HOLD
