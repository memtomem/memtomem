"""Importers report skipped *source* files apart from symlinked *targets*.

A source note that is a symlink, sits under a symlinked directory or resolves
outside the export root is not read. It used to land in ``symlink_paths``,
which the MCP tools render as "target is a symlink", sending the user to the
output directory instead of the vault (#2730).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from helpers import StubCtx
from memtomem.indexing.importers import import_notion, import_obsidian
from memtomem.server.context import AppContext


def _symlink_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks unavailable: {exc}")


def _source_tree(tmp_path: Path) -> Path:
    """A real note, a symlinked note and a note under a symlinked directory."""
    outside = tmp_path / "outside"
    (outside / "dir").mkdir(parents=True)
    (outside / "linked.md").write_text("# Linked\n\nlinked body\n", encoding="utf-8")
    (outside / "dir" / "deep.md").write_text("# Deep\n\ndeep body\n", encoding="utf-8")
    root = tmp_path / "source"
    root.mkdir()
    (root / "real.md").write_text("# Real\n\nreal body\n", encoding="utf-8")
    _symlink_or_skip(root / "linked.md", outside / "linked.md")
    _symlink_or_skip(root / "dir", outside / "dir")
    return root


@pytest.mark.parametrize("importer", [import_obsidian, import_notion])
async def test_skipped_sources_are_not_reported_as_symlinked_targets(
    bm25_only_components, tmp_path, importer
):
    comp, mem_dir = bm25_only_components
    out = mem_dir / "_imported" / "out"
    targets: list[str] = []
    sources: list[str] = []

    imported = await importer(
        _source_tree(tmp_path),
        out,
        index_guard=comp.index_engine,
        symlink_paths=targets,
        source_symlink_paths=sources,
    )

    assert [p.name for p in imported] == ["real.md"]
    assert targets == []
    # Whether the walk descends into the symlinked directory depends on the
    # Python version; when it does, the file under it is skipped as well.
    assert "linked.md" in sources
    assert set(sources) <= {"linked.md", "dir/deep.md"}
    assert not (out / "linked.md").exists()
    assert not (out / "dir").exists()


@pytest.mark.parametrize(("kind", "where"), [("obsidian", "vault"), ("notion", "export")])
async def test_tool_output_names_skipped_sources(bm25_only_components, tmp_path, kind, where):
    from memtomem.server.tools import importers

    comp, _ = bm25_only_components
    ctx = StubCtx(AppContext.from_components(comp))
    tool = getattr(importers, f"mem_import_{kind}")

    result = await tool(str(_source_tree(tmp_path)), ctx=ctx)

    lines = result.splitlines()
    assert "- Files imported: 1" in lines
    assert "- Skipped (target is a symlink): 0" in lines
    skipped = [
        line
        for line in lines
        if line.startswith(f"- Skipped (source is a symlink or outside the {where}): ")
    ]
    assert len(skipped) == 1
    assert int(skipped[0].rsplit(": ", 1)[1]) >= 1
