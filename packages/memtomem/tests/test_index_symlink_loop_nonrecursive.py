"""A non-recursive directory index tolerates a looping link among its entries (#2906).

The non-recursive walk lists every name with a supported extension, so a link
such as ``loop.md -> loop.md`` reaches the bulk paths (the recursive walk drops
it with ``is_file()``). Keeping it listed is deliberate: a previously indexed
file that has since become a loop or a directory must still be visited, so it
is held or purged rather than left searchable.

On Python 3.12 ``Path.resolve()`` raises ``RuntimeError`` for a loop, and three
sites called it on every discovered entry: the stream's namespace prepass, the
stream's per-file call, and ``day_file_declaration`` (reached by both bulk
paths' prepass for a source with no stored namespace). 3.13 and later return a
path instead, so on those versions these tests pass without the fix; CI pins
3.12 on Ubuntu.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from memtomem.memory_scope import day_file_declaration
from memtomem.storage.sqlite_helpers import norm_path
from test_source_holds import _indexed


def _symlink_or_skip(link: Path, target: str) -> None:
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError) as exc:  # Windows without the privilege
        pytest.skip(f"symlinks unavailable: {exc}")


async def _run(components, directory: Path, mode: str) -> dict:
    engine = components.index_engine
    if mode == "plain":
        stats = await engine.index_path(directory, recursive=False)
        return {
            "total_files": stats.total_files,
            "indexed_chunks": stats.indexed_chunks,
            "errors": list(stats.errors),
        }
    events = [ev async for ev in engine.index_path_stream(directory, recursive=False)]
    complete = events[-1]
    assert complete["type"] == "complete"
    return {
        "total_files": complete["total_files"],
        "indexed_chunks": complete["indexed_chunks"],
        "errors": list(complete["errors"]),
    }


def test_day_file_declaration_does_not_raise_for_a_loop(tmp_path):
    _symlink_or_skip(tmp_path / "loop.md", "loop.md")

    assert day_file_declaration(tmp_path / "loop.md") == (None, False)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["plain", "stream"])
async def test_a_fresh_loop_does_not_stop_the_run(components, memory_dir, mode):
    (memory_dir / "ok.md").write_text("# Ok\n\nloopwalkword body.\n", encoding="utf-8")
    _symlink_or_skip(memory_dir / "loop.md", "loop.md")
    _symlink_or_skip(memory_dir / "dangling.md", "missing.md")

    result = await _run(components, memory_dir, mode)

    # The same answer from both bulk paths: three entries visited, the loop
    # and the dangling link indexed as nothing, and no per-file error either.
    assert result == {"total_files": 3, "indexed_chunks": 1, "errors": []}
    hits, _ = await components.search_pipeline.search("loopwalkword")
    assert [Path(h.chunk.metadata.source_file).name for h in hits] == ["ok.md"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["plain", "stream"])
async def test_an_indexed_file_that_became_a_loop_is_held(components, memory_dir, mode):
    path = memory_dir / "loop.md"
    await _indexed(components, path)
    old_ids = await components.storage.get_chunk_hashes(path)
    assert old_ids
    path.unlink()
    _symlink_or_skip(path, "loop.md")

    result = await _run(components, memory_dir, mode)

    assert result["errors"] == []
    assert await components.storage.get_held_sources() == [Path(norm_path(path))]
    assert await components.storage.get_chunk_hashes(path) == old_ids
    assert (await components.search_pipeline.search("mountword"))[0] == []
