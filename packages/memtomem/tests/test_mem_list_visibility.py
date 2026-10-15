"""``mem_list`` hides what a default search would not show.

Two filters run inside the tool itself, ahead of the caller's own:

- sources that are held or have a pending delete check are dropped, whatever
  ``namespace=`` asks for — the chunks are retained for recovery, not listed;
- with no ``namespace=``, a source whose chunks all sit in system namespaces
  (``archive:``, ``agent-runtime:``) is dropped, while one that also holds a
  normal namespace stays. Naming the namespace lists it again.

``TestMemList`` in ``test_server_tools_core.py`` covers the storage query
underneath; these call the tool.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from helpers import StubCtx
from memtomem.models import Chunk, ChunkMetadata
from memtomem.server.context import AppContext
from memtomem.server.tools.browse import mem_list


def _chunk(source: Path, namespace: str) -> Chunk:
    return Chunk(
        content=f"{source.stem} in {namespace}",
        metadata=ChunkMetadata(source_file=source, namespace=namespace),
        content_hash=f"hash-{uuid4().hex}",
        embedding=[0.1] * 1024,
    )


def _listed(output: str) -> set[str]:
    """File names on the tool's per-file rows (``  <path>  — N chunks ...``)."""
    return {
        Path(line.strip().split("  — ")[0]).name
        for line in output.splitlines()
        if line.startswith("  ") and "  — " in line
    }


@pytest.fixture
async def listing(bm25_only_components):
    comp, mem_dir = bm25_only_components
    paths = {
        name: mem_dir / f"{name}.md"
        for name in ("plain", "held", "pending", "archived", "agent", "mixed")
    }
    await comp.storage.upsert_chunks(
        [
            _chunk(paths["plain"], "default"),
            _chunk(paths["held"], "default"),
            _chunk(paths["pending"], "default"),
            _chunk(paths["archived"], "archive:2026"),
            _chunk(paths["agent"], "agent-runtime:planner"),
            _chunk(paths["mixed"], "default"),
            _chunk(paths["mixed"], "archive:2026"),
        ]
    )
    assert await comp.storage.hold_source(paths["held"], "test_hold")
    assert comp.storage.queue_source_check_sync(paths["pending"])
    return StubCtx(AppContext.from_components(comp))


class TestMemListVisibility:
    @pytest.mark.asyncio
    async def test_default_listing_hides_held_pending_and_system_only(self, listing):
        out = await mem_list(ctx=listing)  # type: ignore[arg-type]
        assert _listed(out) == {"plain.md", "mixed.md"}, out

    @pytest.mark.asyncio
    async def test_explicit_system_namespace_lists_its_sources(self, listing):
        out = await mem_list(namespace="archive:2026", ctx=listing)  # type: ignore[arg-type]
        assert _listed(out) == {"archived.md", "mixed.md"}, out

    @pytest.mark.asyncio
    async def test_explicit_namespace_does_not_reveal_held_or_pending(self, listing):
        out = await mem_list(namespace="default", ctx=listing)  # type: ignore[arg-type]
        assert _listed(out) == {"plain.md", "mixed.md"}, out

    @pytest.mark.asyncio
    async def test_nothing_visible_reads_as_no_indexed_files(self, bm25_only_components):
        comp, mem_dir = bm25_only_components
        held = mem_dir / "only.md"
        await comp.storage.upsert_chunks([_chunk(held, "default")])
        assert await comp.storage.hold_source(held, "test_hold")

        out = await mem_list(ctx=StubCtx(AppContext.from_components(comp)))  # type: ignore[arg-type]
        assert out == "No indexed files."
