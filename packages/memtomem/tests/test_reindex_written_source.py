"""The re-index after a memory tool's write reads the file that was written, or nothing (#3320).

A tool reads a file, has it judged, writes it, and asks the engine to index
it, all under the file's sidecar. That lock binds cooperating writers only, so
another process can replace the path between the write and the re-index. The
re-index used to take the path again and resolve it again, and so indexed
whatever the path named by then under the caller's namespace:

1. a parent directory replaced by a link before the re-index resolved anything,
2. a leaf swapped after the relink check under the locks and before the read,
3. a leaf that was a symlink all along and was re-pointed after the append.

``written_source`` names the file by the path the read resolved and the
identity of the descriptor the write went through. The engine opens that path
as given and reads the descriptor only if it is that file.
"""

from __future__ import annotations

import asyncio
import errno
import os
from pathlib import Path
from unittest import mock
from unittest.mock import AsyncMock

import pytest

import memtomem.tools.memory_writer as writer_mod
from memtomem.storage.source_holds import RELINKED_HOLD
from memtomem.storage.sqlite_helpers import norm_path, resolve_path
from memtomem.tools.memory_writer import (
    Appended,
    SourceRemovedError,
    SourceReplacedError,
    WrittenSource,
    append_blocks,
    open_written,
    read_for_append,
)

_MINE = "# Mine\n\n## Plans\n\nalpha falcon body\n"
_MINE_EDITED = "# Mine\n\n## Plans\n\nalpha falcon edited\n"
_OTHER = "# Other\n\n## Plans\n\nbeta heron body\n"

symlinks = pytest.mark.requires_symlinks


def _vectors(texts, **_):
    return [[0.1] * 1024 for _ in texts]


@pytest.fixture
def engine(components):
    """A real store with a stub embedder of positive dimension (receipts need vectors)."""
    embedder = AsyncMock()
    embedder.embed_texts = AsyncMock(side_effect=_vectors)
    embedder.dimension = 1024
    components.index_engine._embedder = embedder
    return components.index_engine


def _rows(storage, key: str) -> list[tuple[str, str]]:
    return [
        (row[0], row[1])
        for row in storage._get_db()
        .execute(
            "SELECT content, namespace FROM chunks WHERE source_file=? ORDER BY content", (key,)
        )
        .fetchall()
    ]


def _reason(storage, key: str) -> str | None:
    row = (
        storage._get_db()
        .execute("SELECT reason FROM held_sources WHERE source_file=?", (key,))
        .fetchone()
    )
    return row[0] if row else None


def _written(path: Path) -> WrittenSource:
    """What a tool that just wrote ``path`` hands the engine: taken before any swap."""
    info = os.stat(path)
    return WrittenSource(resolve_path(path), (info.st_dev, info.st_ino))


async def _indexed(engine, path: Path, text: str, namespace: str) -> str:
    path.write_text(text, encoding="utf-8")
    stats = await engine.index_file(path, namespace=namespace)
    assert not stats.errors, stats.errors
    assert stats.indexed_chunks > 0
    return norm_path(path)


async def _reindex(engine, path: Path, written: WrittenSource, **kwargs):
    return await engine.index_file(
        path, namespace="mine-ns", force=True, lock_held=True, written_source=written, **kwargs
    )


def _assert_held_and_untouched(storage, stats, mine_key: str, other_key: str) -> None:
    """The written file's rows are held as they were; the other file's are as they were."""
    assert stats.relinked_files == 1
    assert stats.indexed_chunks == 0
    assert _reason(storage, mine_key) == RELINKED_HOLD
    assert _rows(storage, mine_key) == [("alpha falcon body", "mine-ns")]
    assert _rows(storage, other_key) == [("beta heron body", "other-ns")]
    assert _reason(storage, other_key) is None


# ---- the three measured cases ------------------------------------------------


@symlinks
async def test_a_parent_relinked_before_the_reindex_is_held(components, engine, memory_dir):
    """Case 1: the engine resolves nothing, so it cannot agree with itself about the new parent."""
    mine_dir, other_dir = memory_dir / "mine", memory_dir / "other"
    mine_dir.mkdir()
    other_dir.mkdir()
    mine, other = mine_dir / "note.md", other_dir / "note.md"
    mine_key = await _indexed(engine, mine, _MINE, "mine-ns")
    other_key = await _indexed(engine, other, _OTHER, "other-ns")

    mine.write_text(_MINE_EDITED, encoding="utf-8")
    written = _written(mine)
    mine_dir.rename(memory_dir / "mine.moved")
    mine_dir.symlink_to(other_dir, target_is_directory=True)

    stats = await _reindex(engine, mine, written)

    _assert_held_and_untouched(components.storage, stats, mine_key, other_key)


@symlinks
@pytest.mark.parametrize("purge", [False, True], ids=["append", "by-id"])
async def test_a_parent_relinked_before_the_reindex_purges_nothing(
    components, engine, memory_dir, purge
):
    """``purge_if_excluded`` keys on the written file, not on the directory the path leads to now."""
    mine_dir, other_dir = memory_dir / "mine", memory_dir / "other"
    mine_dir.mkdir()
    other_dir.mkdir()
    mine, other = mine_dir / "note.md", other_dir / "note.md"
    mine_key = await _indexed(engine, mine, _MINE, "mine-ns")
    other_key = await _indexed(engine, other, _OTHER, "other-ns")
    written = _written(mine)
    mine_dir.rename(memory_dir / "mine.moved")
    mine_dir.symlink_to(other_dir, target_is_directory=True)
    components.config.indexing.exclude_patterns = ["**/other/note.md"]

    stats = await _reindex(engine, mine, written, purge_if_excluded=purge)

    assert stats.deleted_chunks == 0
    _assert_held_and_untouched(components.storage, stats, mine_key, other_key)


@symlinks
async def test_a_leaf_swapped_before_the_reindex_is_held(components, engine, memory_dir):
    mine, other = memory_dir / "mine.md", memory_dir / "other.md"
    mine_key = await _indexed(engine, mine, _MINE, "mine-ns")
    other_key = await _indexed(engine, other, _OTHER, "other-ns")
    mine.write_text(_MINE_EDITED, encoding="utf-8")
    written = _written(mine)
    mine.rename(memory_dir / "mine.aside")
    mine.symlink_to(other)

    stats = await _reindex(engine, mine, written)

    _assert_held_and_untouched(components.storage, stats, mine_key, other_key)


@symlinks
async def test_a_leaf_swapped_after_the_open_is_held(components, engine, memory_dir):
    """Case 2: the read comes off the descriptor, and the path check after it sees the link."""
    mine, other = memory_dir / "mine.md", memory_dir / "other.md"
    mine_key = await _indexed(engine, mine, _MINE, "mine-ns")
    other_key = await _indexed(engine, other, _OTHER, "other-ns")
    mine.write_text(_MINE_EDITED, encoding="utf-8")
    written = _written(mine)
    spec = engine._get_user_exclude_spec

    def swap_then_spec():
        # Runs in ``_index_file`` after the written file was opened, before the read.
        if not mine.is_symlink():
            mine.rename(memory_dir / "mine.aside")
            mine.symlink_to(other)
        return spec()

    with mock.patch.object(engine, "_get_user_exclude_spec", side_effect=swap_then_spec):
        stats = await _reindex(engine, mine, written)

    _assert_held_and_untouched(components.storage, stats, mine_key, other_key)


@symlinks
async def test_a_repointed_link_indexes_the_file_the_append_landed_in(
    components, engine, memory_dir
):
    """Case 3: the write went through the link into ``first``, and that is what is indexed."""
    first, second, link = memory_dir / "first.md", memory_dir / "second.md", memory_dir / "link.md"
    first.write_text(_MINE, encoding="utf-8")
    link.symlink_to(first)
    stats = await engine.index_file(link, namespace="mine-ns")
    assert stats.indexed_chunks > 0
    first_key = norm_path(first)
    second_key = await _indexed(engine, second, _OTHER, "other-ns")

    first.write_text(_MINE_EDITED, encoding="utf-8")
    written = _written(link)
    link.unlink()
    link.symlink_to(second)

    stats = await _reindex(engine, link, written)

    assert stats.relinked_files == 0
    assert _rows(components.storage, first_key) == [("alpha falcon edited", "mine-ns")]
    assert _reason(components.storage, first_key) is None
    assert _rows(components.storage, second_key) == [("beta heron body", "other-ns")]


# ---- files that are the written file are not held ----------------------------------


@symlinks
async def test_a_link_that_still_leads_to_the_written_file_is_indexed(
    components, engine, memory_dir
):
    target, link = memory_dir / "target.md", memory_dir / "link.md"
    target.write_text(_MINE_EDITED, encoding="utf-8")
    link.symlink_to(target)

    stats = await _reindex(engine, link, _written(link))

    assert stats.relinked_files == 0
    assert _rows(components.storage, norm_path(target)) == [("alpha falcon edited", "mine-ns")]


@symlinks
async def test_a_path_through_a_linked_directory_is_indexed(
    components, engine, memory_dir, tmp_path
):
    """The spelling the caller used need not be the resolution the token carries."""
    real = memory_dir / "real"
    real.mkdir()
    (tmp_path / "alias").symlink_to(real, target_is_directory=True)
    spelled = tmp_path / "alias" / "note.md"
    spelled.write_text(_MINE_EDITED, encoding="utf-8")

    stats = await _reindex(engine, spelled, _written(spelled))

    assert stats.relinked_files == 0
    assert _rows(components.storage, norm_path(real / "note.md")) == [
        ("alpha falcon edited", "mine-ns")
    ]


async def test_a_file_reached_through_a_second_hard_link_is_indexed(components, engine, memory_dir):
    mine = memory_dir / "mine.md"
    mine.write_text(_MINE_EDITED, encoding="utf-8")
    try:
        os.link(mine, memory_dir / "twin.md")
    except OSError:
        pytest.skip("hard links are not available here")

    stats = await _reindex(engine, mine, _written(mine))

    assert stats.relinked_files == 0
    assert _rows(components.storage, norm_path(mine)) == [("alpha falcon edited", "mine-ns")]


# ---- other things found at the path ------------------------------------------------


async def test_another_file_saved_over_the_path_is_held(components, engine, memory_dir):
    """A save-via-rename since the write: the write is on the inode that was renamed away."""
    mine = memory_dir / "mine.md"
    mine_key = await _indexed(engine, mine, _MINE, "mine-ns")
    mine.write_text(_MINE_EDITED, encoding="utf-8")
    written = _written(mine)
    fresh = memory_dir / "fresh.tmp"
    fresh.write_text(_OTHER, encoding="utf-8")
    mine.rename(memory_dir / "mine.aside")
    fresh.rename(mine)

    stats = await _reindex(engine, mine, written)

    assert stats.relinked_files == 1
    assert _reason(components.storage, mine_key) == RELINKED_HOLD
    assert _rows(components.storage, mine_key) == [("alpha falcon body", "mine-ns")]


@symlinks
async def test_a_removed_file_is_held_as_missing_under_the_written_key(
    components, engine, memory_dir
):
    """Removed, and its directory now a link elsewhere: the hold still lands on the written key."""
    mine_dir, other_dir = memory_dir / "mine", memory_dir / "other"
    mine_dir.mkdir()
    other_dir.mkdir()
    mine = mine_dir / "note.md"
    mine_key = await _indexed(engine, mine, _MINE, "mine-ns")
    written = _written(mine)
    mine.unlink()
    mine_dir.rename(memory_dir / "mine.moved")
    mine_dir.symlink_to(other_dir, target_is_directory=True)

    stats = await _reindex(engine, mine, written)

    assert stats.relinked_files == 0
    assert _reason(components.storage, mine_key) == "source_missing"
    assert _reason(components.storage, norm_path(other_dir / "note.md")) is None


async def test_a_removed_file_under_a_retain_dir_stays_shown(components, engine, memory_dir):
    mine = memory_dir / "mine.md"
    mine_key = await _indexed(engine, mine, _MINE, "mine-ns")
    written = _written(mine)
    mine.unlink()
    engine._config.retain_missing_dirs = [memory_dir]

    stats = await _reindex(engine, mine, written)

    assert stats.relinked_files == 0
    assert _reason(components.storage, mine_key) is None
    assert _rows(components.storage, mine_key) == [("alpha falcon body", "mine-ns")]


# ---- exclusion -----------------------------------------------------------------------


@symlinks
@pytest.mark.parametrize("purge", [False, True], ids=["append", "by-id"])
async def test_a_link_to_an_excluded_file_put_in_after_the_open_is_held(
    components, engine, memory_dir, purge
):
    """The exclusion answer is about the link's target, so it is not taken as an exclusion."""
    mine, skipped = memory_dir / "mine.md", memory_dir / "skipme.md"
    mine_key = await _indexed(engine, mine, _MINE, "mine-ns")
    skipped.write_text(_OTHER, encoding="utf-8")
    components.config.indexing.exclude_patterns = ["**/skipme.md"]
    mine.write_text(_MINE_EDITED, encoding="utf-8")
    written = _written(mine)
    spec = engine._get_user_exclude_spec

    def swap_then_spec():
        if not mine.is_symlink():
            mine.rename(memory_dir / "mine.aside")
            mine.symlink_to(skipped)
        return spec()

    with mock.patch.object(engine, "_get_user_exclude_spec", side_effect=swap_then_spec):
        stats = await _reindex(engine, mine, written, purge_if_excluded=purge)

    assert stats.relinked_files == 1
    assert stats.excluded_files == 0
    assert stats.deleted_chunks == 0
    assert _reason(components.storage, mine_key) == RELINKED_HOLD
    assert _rows(components.storage, mine_key) == [("alpha falcon body", "mine-ns")]


@pytest.mark.parametrize("purge", [False, True], ids=["append", "by-id"])
async def test_an_exclusion_of_the_written_file_itself_is_still_an_exclusion(
    components, engine, memory_dir, purge
):
    mine = memory_dir / "mine.md"
    mine_key = await _indexed(engine, mine, _MINE, "mine-ns")
    mine.write_text(_MINE_EDITED, encoding="utf-8")
    components.config.indexing.exclude_patterns = ["**/mine.md"]

    stats = await _reindex(engine, mine, _written(mine), purge_if_excluded=purge)

    assert stats.relinked_files == 0
    assert stats.excluded_files == 1
    assert _reason(components.storage, mine_key) is None
    expected = [] if purge else [("alpha falcon body", "mine-ns")]
    assert _rows(components.storage, mine_key) == expected


# ---- a swap while the pass awaits ------------------------------------------------------


@symlinks
async def test_a_leaf_swapped_during_the_embedding_is_held(components, engine, memory_dir):
    mine, other = memory_dir / "mine.md", memory_dir / "other.md"
    mine_key = await _indexed(engine, mine, _MINE, "mine-ns")
    other_key = await _indexed(engine, other, _OTHER, "other-ns")
    mine.write_text(_MINE_EDITED, encoding="utf-8")
    written = _written(mine)
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocking(texts, **_):
        entered.set()
        await asyncio.wait_for(release.wait(), 10)
        return _vectors(texts)

    engine._embedder.embed_texts = AsyncMock(side_effect=blocking)
    task = asyncio.ensure_future(_reindex(engine, mine, written))
    await asyncio.wait_for(entered.wait(), 10)
    mine.rename(memory_dir / "mine.aside")
    mine.symlink_to(other)
    release.set()
    stats = await asyncio.wait_for(task, 10)

    _assert_held_and_untouched(components.storage, stats, mine_key, other_key)


@symlinks
async def test_a_link_to_a_hard_link_of_the_written_file_is_held(components, engine, memory_dir):
    """Same inode, so the open passes; the path names another key, so the check after it does not."""
    mine, twin = memory_dir / "mine.md", memory_dir / "twin.md"
    mine_key = await _indexed(engine, mine, _MINE, "mine-ns")
    mine.write_text(_MINE_EDITED, encoding="utf-8")
    written = _written(mine)
    try:
        os.link(mine, twin)
    except OSError:
        pytest.skip("hard links are not available here")
    mine.unlink()
    mine.symlink_to(twin)

    stats = await _reindex(engine, mine, written)

    assert stats.relinked_files == 1
    assert _reason(components.storage, mine_key) == RELINKED_HOLD
    assert _rows(components.storage, mine_key) == [("alpha falcon body", "mine-ns")]
    assert _rows(components.storage, norm_path(twin)) == []


# ---- errors after the open keep the written key -----------------------------------------


@symlinks
async def test_a_stat_error_holds_the_written_key_not_what_the_path_names(
    components, engine, memory_dir
):
    mine, other = memory_dir / "mine.md", memory_dir / "other.md"
    mine_key = await _indexed(engine, mine, _MINE, "mine-ns")
    other_key = await _indexed(engine, other, _OTHER, "other-ns")
    written = _written(mine)
    real_fstat = os.fstat
    calls = []

    def fstat(fd):
        calls.append(fd)
        if len(calls) == 1:
            # The open's identity check.
            return real_fstat(fd)
        mine.rename(memory_dir / "mine.aside")
        mine.symlink_to(other)
        raise OSError(errno.EIO, "injected")

    with mock.patch.object(os, "fstat", side_effect=fstat):
        stats = await _reindex(engine, mine, written)

    assert stats.indexed_chunks == 0
    assert _reason(components.storage, mine_key) == f"stat_error:{errno.EIO}"
    assert _reason(components.storage, other_key) is None


@symlinks
async def test_a_read_error_holds_the_written_key_not_what_the_path_names(
    components, engine, memory_dir
):
    mine, other = memory_dir / "mine.md", memory_dir / "other.md"
    mine_key = await _indexed(engine, mine, _MINE, "mine-ns")
    other_key = await _indexed(engine, other, _OTHER, "other-ns")
    written = _written(mine)
    import memtomem.indexing.engine as engine_mod

    def failing(opened, name):
        opened.close()
        mine.rename(memory_dir / "mine.aside")
        mine.symlink_to(other)
        raise OSError(errno.EIO, "injected")

    with mock.patch.object(engine_mod, "_opened_text", side_effect=failing):
        stats = await _reindex(engine, mine, written)

    assert stats.indexed_chunks == 0
    assert _reason(components.storage, mine_key) == f"read_error:{errno.EIO}"
    assert _reason(components.storage, other_key) is None


# ---- a filesystem that gives no identity -------------------------------------------------


@symlinks
async def test_without_an_identity_a_link_left_at_the_path_is_held(components, engine, memory_dir):
    mine, other = memory_dir / "mine.md", memory_dir / "other.md"
    mine_key = await _indexed(engine, mine, _MINE, "mine-ns")
    other_key = await _indexed(engine, other, _OTHER, "other-ns")
    written = WrittenSource(resolve_path(mine), None)
    mine.rename(memory_dir / "mine.aside")
    mine.symlink_to(other)

    stats = await _reindex(engine, mine, written)

    _assert_held_and_untouched(components.storage, stats, mine_key, other_key)


async def test_without_an_identity_the_written_file_is_indexed(components, engine, memory_dir):
    mine = memory_dir / "mine.md"
    mine.write_text(_MINE_EDITED, encoding="utf-8")

    stats = await _reindex(engine, mine, WrittenSource(resolve_path(mine), None))

    assert stats.relinked_files == 0
    assert _rows(components.storage, norm_path(mine)) == [("alpha falcon edited", "mine-ns")]


# ---- the descriptor read decodes as the path read does -----------------------------------


@pytest.mark.parametrize(
    "data",
    [
        b"# T\r\n\r\n## Plans\r\n\r\nalpha falcon\r\nsecond line\r\n",
        b"# T\r\r## Plans\r\ralpha falcon\rsecond line\r",
        b"# T\n\n## Plans\n\nalpha \xff\xfe falcon\n",
    ],
    ids=["crlf", "lone-cr", "invalid-utf8"],
)
async def test_the_descriptor_read_stores_what_the_path_read_stores(
    components, engine, memory_dir, data
):
    by_path, by_descriptor = memory_dir / "path.md", memory_dir / "descriptor.md"
    by_path.write_bytes(data)
    by_descriptor.write_bytes(data)

    await engine.index_file(by_path, namespace="mine-ns")
    stats = await _reindex(engine, by_descriptor, _written(by_descriptor))

    def stored(path: Path):
        return (
            components.storage._get_db()
            .execute(
                "SELECT content, content_hash, start_line, end_line FROM chunks"
                " WHERE source_file=? ORDER BY start_line",
                (norm_path(path),),
            )
            .fetchall()
        )

    assert stats.indexed_chunks > 0
    assert stored(by_descriptor) == stored(by_path) != []


# ---- arguments ------------------------------------------------------------------------------


async def test_written_source_needs_the_callers_lock(engine, memory_dir):
    mine = memory_dir / "mine.md"
    mine.write_text(_MINE, encoding="utf-8")
    with pytest.raises(ValueError, match="lock_held"):
        await engine.index_file(mine, written_source=_written(mine))


async def test_written_source_replaces_hold_if_relinked(engine, memory_dir):
    mine = memory_dir / "mine.md"
    mine.write_text(_MINE, encoding="utf-8")
    with pytest.raises(ValueError, match="hold_if_relinked"):
        await engine.index_file(
            mine, lock_held=True, hold_if_relinked=True, written_source=_written(mine)
        )


# ---- open_written ---------------------------------------------------------------------------


def test_open_written_gives_the_bytes_of_the_written_file(tmp_path):
    mine = tmp_path / "mine.md"
    mine.write_bytes(b"one\r\ntwo\r\n")
    with open_written(_written(mine)) as handle:
        assert handle.read() == b"one\r\ntwo\r\n"


def test_open_written_refuses_another_file_at_the_path(tmp_path):
    mine = tmp_path / "mine.md"
    mine.write_text("mine", encoding="utf-8")
    written = _written(mine)
    mine.rename(tmp_path / "aside.md")
    mine.write_text("other", encoding="utf-8")
    with pytest.raises(SourceReplacedError, match="it was not indexed"):
        open_written(written)


def test_open_written_says_removed_when_nothing_is_there(tmp_path):
    mine = tmp_path / "mine.md"
    mine.write_text("mine", encoding="utf-8")
    written = _written(mine)
    mine.unlink()
    with pytest.raises(SourceRemovedError, match="before it was re-indexed"):
        open_written(written)


def test_open_written_refuses_a_directory_at_the_path(tmp_path):
    mine = tmp_path / "mine.md"
    mine.write_text("mine", encoding="utf-8")
    written = _written(mine)
    mine.unlink()
    mine.mkdir()
    with pytest.raises(SourceReplacedError):
        open_written(written)


@symlinks
def test_open_written_refuses_a_link_to_itself(tmp_path):
    mine = tmp_path / "mine.md"
    mine.write_text("mine", encoding="utf-8")
    written = _written(mine)
    mine.unlink()
    mine.symlink_to(mine)
    with pytest.raises(SourceReplacedError):
        open_written(written)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs a FIFO")
def test_open_written_refuses_a_fifo_without_waiting_for_a_writer(tmp_path):
    mine = tmp_path / "mine.md"
    mine.write_text("mine", encoding="utf-8")
    written = _written(mine)
    mine.unlink()
    os.mkfifo(mine)
    with pytest.raises(SourceReplacedError):
        open_written(written)


# ---- what an append hands on -----------------------------------------------------------------


def test_an_append_names_the_file_it_created(tmp_path):
    target = tmp_path / "new" / "note.md"
    judged = read_for_append(target)
    assert judged.existed is False and judged.identity is None

    appended = append_blocks(target, ["\n## One\n\nbody\n"], judged=judged)

    info = os.stat(target)
    assert appended == Appended(
        "\n## One\n\nbody\n", WrittenSource(resolve_path(target), (info.st_dev, info.st_ino))
    )


def test_an_append_names_the_file_it_read(tmp_path):
    target = tmp_path / "note.md"
    target.write_text("# Note\n", encoding="utf-8")
    judged = read_for_append(target)

    appended = append_blocks(target, ["\n## One\n\nbody\n"], judged=judged)

    assert appended.source == WrittenSource(judged.source_path, judged.identity)
    assert appended.source.identity is not None


def test_an_empty_append_names_what_was_read_and_writes_nothing(tmp_path):
    target = tmp_path / "note.md"
    judged = read_for_append(target)

    appended = append_blocks(target, [], judged=judged)

    assert appended == Appended("", WrittenSource(judged.source_path, None))
    assert not target.exists()


def test_an_append_on_a_filesystem_without_inodes_names_no_identity(tmp_path):
    target = tmp_path / "note.md"
    target.write_text("# Note\n", encoding="utf-8")
    real_fstat = os.fstat

    def no_inode(fd):
        info = real_fstat(fd)
        return os.stat_result((info.st_mode, 0, *tuple(info)[2:]))

    with mock.patch.object(writer_mod.os, "fstat", side_effect=no_inode):
        judged = read_for_append(target)
        appended = append_blocks(target, ["\n## One\n\nbody\n"], judged=judged)

    assert judged.identity is None
    assert appended.source.identity is None
    assert target.read_text(encoding="utf-8").endswith("body\n")


async def test_a_file_an_append_created_and_lost_its_path_is_held(components, engine, memory_dir):
    """The identity of a created file comes from the append, so a swap after it is seen."""
    target = memory_dir / "fresh.md"
    judged = read_for_append(target)
    appended = append_blocks(target, ["\n## One\n\nalpha falcon body\n"], judged=judged)
    target.rename(memory_dir / "fresh.aside")
    target.write_text(_OTHER, encoding="utf-8")

    stats = await _reindex(engine, target, appended.source)

    assert stats.relinked_files == 1
    assert _rows(components.storage, norm_path(target)) == []


# ---- no identity: the descriptor's type is all there is to compare ---------------------------


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs a FIFO")
def test_without_an_identity_open_written_refuses_a_fifo(tmp_path):
    """Only the regular-file check sees it: with an identity, the FIFO's own would differ."""
    mine = tmp_path / "mine.md"
    os.mkfifo(mine)
    with pytest.raises(SourceReplacedError):
        open_written(WrittenSource(resolve_path(mine), None))


def test_without_an_identity_open_written_refuses_a_directory(tmp_path):
    mine = tmp_path / "mine.md"
    mine.mkdir()
    with pytest.raises(SourceReplacedError):
        open_written(WrittenSource(resolve_path(mine), None))


@symlinks
async def test_a_purge_through_a_link_deletes_the_written_files_rows(
    components, engine, memory_dir
):
    """The purge key is the written file's: the link's own key holds no rows to delete."""
    target, link = memory_dir / "target.md", memory_dir / "link.md"
    target_key = await _indexed(engine, target, _MINE, "mine-ns")
    link.symlink_to(target)
    target.write_text(_MINE_EDITED, encoding="utf-8")
    components.config.indexing.exclude_patterns = ["**/target.md"]

    stats = await _reindex(engine, link, _written(link), purge_if_excluded=True)

    assert stats.excluded_files == 1
    assert stats.deleted_chunks == 1
    assert _rows(components.storage, target_key) == []


@symlinks
async def test_a_removed_file_is_judged_for_retention_by_the_written_key(
    components, engine, memory_dir
):
    """Its directory now leads outside the retain dir; the file that was written was inside it."""
    keep, outside = memory_dir / "keep", memory_dir / "outside"
    (keep / "mine").mkdir(parents=True)
    outside.mkdir()
    mine = keep / "mine" / "note.md"
    mine_key = await _indexed(engine, mine, _MINE, "mine-ns")
    written = _written(mine)
    engine._config.retain_missing_dirs = [keep]
    mine.unlink()
    (keep / "mine").rename(memory_dir / "mine.moved")
    (keep / "mine").symlink_to(outside, target_is_directory=True)

    stats = await _reindex(engine, mine, written)

    assert stats.relinked_files == 0
    assert _reason(components.storage, mine_key) is None
    assert _rows(components.storage, mine_key) == [("alpha falcon body", "mine-ns")]


async def test_invalid_bytes_are_replaced_from_the_descriptor_not_reread_by_path(
    components, engine, memory_dir
):
    """The path read's fallback reads the path a second time; this one has the bytes already."""
    mine = memory_dir / "mine.md"
    mine.write_bytes(b"# Mine\n\n## Plans\n\nalpha \xff falcon\n")
    written = _written(mine)
    spec = engine._get_user_exclude_spec

    def replace_then_spec():
        # After the open: another regular file at the same path, so the key is unchanged.
        if not (memory_dir / "mine.aside").exists():
            mine.rename(memory_dir / "mine.aside")
            mine.write_text(_OTHER, encoding="utf-8")
        return spec()

    with mock.patch.object(engine, "_get_user_exclude_spec", side_effect=replace_then_spec):
        stats = await _reindex(engine, mine, written)

    assert stats.indexed_chunks == 1
    assert _rows(components.storage, norm_path(mine)) == [("alpha � falcon", "mine-ns")]
