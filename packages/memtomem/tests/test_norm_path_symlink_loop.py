"""A source that is a symlink loop no longer crashes on Python 3.12 (#2841).

On 3.12 ``Path.resolve()`` turns a loop into ``RuntimeError``; 3.13 and later
return a path. ``norm_path`` let the 3.12 error escape, so a looping source
crashed the engine, the hold, the purge and doctor instead of being held as an
unavailable source. The shapes pinned here resolve the same on every Python;
other loops need not (see ``resolve_path``).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from memtomem.storage.sqlite_helpers import norm_path
from test_source_holds import _indexed


def _symlink_or_skip(link: Path, target: str) -> None:
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError) as exc:  # Windows without the privilege
        pytest.skip(f"symlinks unavailable: {exc}")


def _loop_in_place(path: Path) -> None:
    """Replace ``path`` with a link to itself."""
    path.unlink()
    _symlink_or_skip(path, path.name)


@pytest.mark.parametrize(
    ("links", "spelled"),
    [
        ({"self.md": "self.md"}, "self.md"),
        ({"a.md": "b.md", "b.md": "a.md"}, "a.md"),
        ({"x": "x"}, os.path.join("x", "child.md")),
    ],
    ids=["self", "cycle", "loop-as-directory"],
)
def test_a_loop_keeps_its_resolved_ancestors(tmp_path, links, spelled):
    # The loop sits under a linked directory, so a fallback that skipped
    # resolution altogether would key it under ``linked`` instead of ``real``.
    real = tmp_path / "real"
    real.mkdir()
    _symlink_or_skip(tmp_path / "linked", "real")
    for name, target in links.items():
        _symlink_or_skip(real / name, target)

    assert norm_path(tmp_path / "linked" / spelled) == str(Path(norm_path(real)) / spelled)


@pytest.mark.asyncio
async def test_index_file_holds_a_source_that_became_a_loop(components, memory_dir):
    path = memory_dir / "loop.md"
    await _indexed(components, path)
    old_ids = await components.storage.get_chunk_hashes(path)
    assert old_ids
    _loop_in_place(path)

    await components.index_engine.index_file(path)

    # Held under the key it was indexed with: the chunks are kept and hidden.
    assert await components.storage.get_held_sources() == [Path(norm_path(path))]
    assert await components.storage.get_chunk_hashes(path) == old_ids
    assert (await components.search_pipeline.search("mountword"))[0] == []


@pytest.mark.asyncio
async def test_delete_by_source_removes_the_rows_of_a_loop(components, memory_dir):
    path = memory_dir / "loop.md"
    await _indexed(components, path)
    count = len(await components.storage.get_chunk_hashes(path))
    _loop_in_place(path)

    assert await components.storage.delete_by_source(path) == count
    assert await components.storage.get_chunk_hashes(path) == {}
