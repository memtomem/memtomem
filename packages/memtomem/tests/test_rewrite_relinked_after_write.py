"""A by-id rewrite whose file is replaced by a symlink before its re-index (#3172).

``mem_edit``, ``mem_delete`` and the web edit and delete routes refuse a key
that already names another file (#2923), then write and re-index. A swap of the
written file for a symlink in between made ``index_file`` resolve the link and
index its target under the target's key, and the pre-rewrite chunks under the
file's own key stayed searchable. ``hold_if_relinked`` holds that key instead.
"""

from __future__ import annotations

import sys
import unicodedata
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from helpers import StubCtx
from memtomem.server.context import AppContext
from memtomem.server.tools import memory_crud
from memtomem.source_provenance import relinked_after_write_notice
from memtomem.storage.source_holds import RELINKED_HOLD
from memtomem.storage.sqlite_helpers import norm_path
from memtomem.tools import memory_writer
from memtomem.web.app import create_app
from memtomem.web.deps import require_configured
from memtomem.web.routes import chunks as chunk_routes

SURFACES = ("mcp_edit", "mcp_delete", "web_edit", "web_delete")

THREE_SECTIONS = (
    "## Alpha\n\nAlpha body one.\n\n"
    "## Bravo\n\nBravo body two.\n\n"
    "## Charlie\n\nCharlie body three.\n"
)
OTHER = "## Other\n\nOtherword body.\n"


def _link_or_skip(link: Path, target: Path) -> None:
    link.unlink(missing_ok=True)
    try:
        link.symlink_to(target)
    except OSError as exc:  # Windows without symlink privilege
        pytest.skip(f"cannot create symlink: {exc}")


def _probe_symlinks(tmp_path: Path) -> None:
    probe = tmp_path / "probe"
    probe.write_text("", encoding="utf-8")
    _link_or_skip(probe, tmp_path)
    probe.unlink()


def _rows(comp, key: str) -> list[str]:
    """Contents stored under exactly ``key``.

    By the key itself: ``list_chunks_by_source`` resolves its argument, which
    for a relinked leaf reads the link target's rows.
    """
    rows = (
        comp.storage._get_db()
        .execute("SELECT content FROM chunks WHERE source_file=?", (key,))
        .fetchall()
    )
    return [r[0] for r in rows]


def _reason(comp, key: str) -> str | None:
    row = (
        comp.storage._get_db()
        .execute("SELECT reason FROM held_sources WHERE source_file=?", (key,))
        .fetchone()
    )
    return row[0] if row else None


async def _hits(comp, query: str) -> list[str]:
    results, _ = await comp.search_pipeline.search(query, top_k=10)
    return [r.chunk.content for r in results]


async def _three_chunk_source(comp, mem_dir: Path):
    """``note.md`` with three chunks, the first the rewrite's target; ``other.md`` unindexed.

    Three chunks, not one: the web delete removes its target row directly
    (``ensure_index_row_absent``), so the siblings are what only the hold hides.
    """
    source = mem_dir / "note.md"
    source.write_text(THREE_SECTIONS, encoding="utf-8")
    stats = await comp.index_engine.index_file(source, ingress_guarded=True)
    assert not stats.errors
    chunks = sorted(
        await comp.storage.list_chunks_by_source(source), key=lambda c: c.metadata.start_line
    )
    assert len(chunks) == 3, [c.content for c in chunks]
    other = mem_dir / "other.md"
    other.write_text(OTHER, encoding="utf-8")
    return source, chunks[0], other


def _relink_inside_the_write(monkeypatch, leaf: Path, target: Path) -> None:
    """Replace ``leaf`` with a link to ``target`` once the writer has written it."""
    for module, name in (
        (memory_writer, "replace_chunk_body"),
        (memory_writer, "remove_lines"),
        (chunk_routes, "replace_chunk_body"),
        (chunk_routes, "remove_lines"),
    ):
        real = getattr(module, name)

        def write(*args, _real=real, **kwargs):
            written = _real(*args, **kwargs)
            _link_or_skip(leaf, target)
            return written

        monkeypatch.setattr(module, name, write)


async def _request(comp, surface, chunk_id):
    if surface.startswith("mcp"):
        ctx = StubCtx(AppContext.from_components(comp))
        if surface.endswith("edit"):
            return await memory_crud.mem_edit(str(chunk_id), "NEW BODY", ctx=ctx)
        return await memory_crud.mem_delete(chunk_id=str(chunk_id), ctx=ctx)
    app = create_app(lifespan=None, mode="dev")
    for name in ("storage", "index_engine", "search_pipeline", "config"):
        setattr(app.state, name, getattr(comp, name))
    app.dependency_overrides[require_configured] = lambda: None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        if surface.endswith("edit"):
            return await client.patch(f"/api/chunks/{chunk_id}", json={"new_content": "NEW BODY"})
        return await client.delete(f"/api/chunks/{chunk_id}")


@pytest.mark.parametrize("surface", SURFACES)
async def test_a_file_relinked_before_its_reindex_is_held_and_its_target_not_indexed(
    bm25_only_components, tmp_path, monkeypatch, surface
):
    comp, mem_dir = bm25_only_components
    _probe_symlinks(tmp_path)
    source, target, other = await _three_chunk_source(comp, mem_dir)
    leaf_key, other_key = str(target.metadata.source_file), norm_path(other)
    assert len(_rows(comp, leaf_key)) == 3
    # Searched first, so a cached result could answer the search below.
    assert await _hits(comp, "Bravo")
    _relink_inside_the_write(monkeypatch, source, other)

    result = await _request(comp, surface, target.id)

    assert source.is_symlink()
    assert other.read_text(encoding="utf-8") == OTHER
    # The rows under the file's own key are held, not served.
    assert _reason(comp, leaf_key) == RELINKED_HOLD
    assert await _hits(comp, "Bravo") == []
    assert await _hits(comp, "Charlie") == []
    # Nothing was indexed through the link.
    assert _rows(comp, other_key) == []
    assert await _hits(comp, "Otherword") == []
    op = "edit" if surface.endswith("edit") else "delete"
    notice = relinked_after_write_notice(op, leaf_key)
    assert "held (hidden from search)" in notice
    if isinstance(result, str):
        assert not result.startswith("Error"), result
        assert result.endswith(f"\n- {notice}"), result
    else:
        assert result.status_code == 200, result.text
        assert result.json()["warning"] == notice
        if op == "edit":
            # The echo is the retained, held pre-edit row.
            assert result.json()["content"] == target.content


async def test_a_rollback_after_the_hold_writes_nothing_into_the_link_target(
    bm25_only_components, tmp_path, monkeypatch
):
    """A failure after the hold restores nothing through the link; the hold stays."""
    comp, mem_dir = bm25_only_components
    _probe_symlinks(tmp_path)
    source, target, other = await _three_chunk_source(comp, mem_dir)
    leaf_key = str(target.metadata.source_file)
    _relink_inside_the_write(monkeypatch, source, other)

    async def boom(*_args, **_kwargs):
        raise RuntimeError("after the re-index")

    monkeypatch.setattr(memory_crud, "flag_untracked_write", boom)

    result = await _request(comp, "mcp_edit", target.id)

    assert result.startswith("Error"), result
    assert source.is_symlink()
    # The restore compares the descriptor's identity with the pre-image's, so
    # the link's target is not overwritten with the pre-edit bytes.
    assert other.read_text(encoding="utf-8") == OTHER
    assert _reason(comp, leaf_key) == RELINKED_HOLD
    assert await _hits(comp, "Bravo") == []


@pytest.mark.skipif(sys.platform != "darwin", reason="keys are Unicode-folded on macOS only")
def test_the_notice_does_not_claim_a_hold_for_a_key_stored_before_folding():
    """A legacy NFD key holds no rows (#2907), so the notice must not say they are hidden."""
    nfc = "/memories/café.md"
    nfd = unicodedata.normalize("NFD", nfc)
    assert nfd != nfc

    folded = relinked_after_write_notice("edit", nfc)
    legacy = relinked_after_write_notice("edit", nfd)

    assert "held (hidden from search)" in folded
    assert "held" not in legacy and "hidden from search" not in legacy
    assert "stay searchable" in legacy
