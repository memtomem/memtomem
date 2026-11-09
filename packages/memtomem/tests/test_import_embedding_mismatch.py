"""A Notion or Obsidian re-import in embedding-mismatch mode writes nothing (#3162).

While ``storage.embedding_mismatch`` is set, ``IndexEngine`` refuses to index
any file. The importers write every target before they index any of them, so a
re-import overwrote each previously imported file, indexed none, left the old
chunks searchable beside the new bytes, and reported ``import complete``. The
handlers now refuse before writing, the way ``mem_index`` and ``mem_import`` do.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from helpers import StubCtx
from memtomem.server.context import AppContext
from memtomem.server.tools import importers

_UUID = "0123456789abcdef0123456789abcdef"


def _source(tmp_path: Path, kind: str, body: str) -> Path:
    root = tmp_path / f"{kind}-source"
    root.mkdir(exist_ok=True)
    name = f"Page {_UUID}.md" if kind == "notion" else "note.md"
    (root / name).write_text(f"# Page\n\n{body}\n", encoding="utf-8")
    return root


def _snapshot(root: Path) -> dict[str, bytes]:
    """The imported markdown files; the writer's empty ``.<name>.lock`` sidecars are left out."""
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*.md")}


def _rows(comp) -> list[tuple[str, str]]:
    db = comp.storage._get_db()
    return db.execute("SELECT source_file, content FROM chunks ORDER BY id").fetchall()


@pytest.mark.parametrize("kind", ["notion", "obsidian"])
async def test_reimport_in_mismatch_mode_writes_nothing_and_says_why(
    bm25_only_components, tmp_path, kind
):
    comp, mem_dir = bm25_only_components
    ctx = StubCtx(AppContext.from_components(comp))
    tool = getattr(importers, f"mem_import_{kind}")
    label = kind.capitalize()
    out = mem_dir / "_imported" / kind

    first = await tool(str(_source(tmp_path, kind, "first import body")), ctx=ctx)
    assert first.startswith(f"{label} import complete:"), first
    disk_before = _snapshot(out)
    rows_before = _rows(comp)
    assert len(disk_before) == 1
    assert any("first import body" in content for _, content in rows_before)

    # The state ``SqliteBackend`` records at open when the configured embedding
    # differs from the stored vectors.
    comp.storage._dim_mismatch = (1024, 384)
    comp.storage._model_mismatch = ("onnx", "bge-m3", "onnx", "bge-small-en-v1.5")

    result = await tool(str(_source(tmp_path, kind, "second import body")), ctx=ctx)

    assert _snapshot(out) == disk_before
    assert _rows(comp) == rows_before
    first_line, _, rest = result.partition("\n")
    assert first_line == f"{label} import wrote nothing."
    assert rest.startswith("Embedding mismatch detected — indexing blocked."), result
