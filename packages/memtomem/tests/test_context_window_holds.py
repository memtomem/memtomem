"""Source holds on the two context-window surfaces (#3114).

Ranked search hides a row under any source hold or a pending source check,
and a consolidation-policy summary whose *parent* is. A search's context
window has to hide the same rows, and leave them out of the position it
reports, or a row search refused to return comes back as the context of the
row next to it.

``mem_expand`` is the deliberate contrast: it starts from an id, and a row
held for anything but its content stays readable by id for recovery
(ADR-0036). Its neighbours keep showing such rows, as its anchor does; only a
redaction hold hides them there (``test_redaction_held_id_access.py``).

The derived case is the one that reaches a caller organically: the anchor is
an ordinary row of a ``.consolidated.md`` file, so nothing hides it, while the
policy row beside it follows its parent's hold.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from helpers import StubCtx
from memtomem.models import ORIGIN_CONSOLIDATION_POLICY
from memtomem.server.context import AppContext
from memtomem.server.tools import search
from memtomem.storage.source_holds import (
    REDACTION_BLOCKED_HOLD,
    RELINKED_HOLD,
    SCOPE_LOST_HOLD,
)

pytestmark = pytest.mark.asyncio

PENDING = "pending"
# Every way ranked search hides a row by its source except the redaction
# hold: an absence hold, the two presence-kept holds that are not about the
# content, and a pending check. Search hides all of them; ``mem_expand`` none.
NOT_WITHHELD = ["scan_missing", RELINKED_HOLD, SCOPE_LOST_HOLD, PENDING]
# The redaction hold is the control, hidden on both surfaces before this fix.
HIDES = [*NOT_WITHHELD, REDACTION_BLOCKED_HOLD]
AVAILABILITY = ["scan_missing", PENDING]


async def _hide(comp, path: Path, how: str) -> None:
    if how == PENDING:
        assert comp.storage.queue_source_check_sync(path)
    else:
        assert await comp.storage.hold_source(path, how)


async def _index_sections(comp, path: Path, tokens: list[str]):
    """One chunk per token, in file order (each section is long enough to split)."""
    body = "".join(f"## S{i}\n\n{tok} " + "word " * 300 + "\n\n" for i, tok in enumerate(tokens))
    path.write_text(f"# F\n\n{body}", encoding="utf-8")
    assert not (await comp.index_engine.index_file(path)).errors
    rows = await comp.storage.list_chunks_by_source(path.resolve())
    by_token = {tok: next(r for r in rows if tok in r.content) for tok in tokens}
    assert len(rows) == len(tokens), [r.content[:20] for r in rows]
    return by_token


async def _consolidated(comp, mem_dir: Path, tokens: list[str], policy: set[str]):
    """``notes.md`` plus its ``.consolidated.md``, the ``policy`` rows marked as summaries."""
    parent = mem_dir / "notes.md"
    parent.write_text("# P\n\nparentword fact.\n", encoding="utf-8")
    assert not (await comp.index_engine.index_file(parent)).errors
    by_token = await _index_sections(comp, mem_dir / "notes.md.consolidated.md", tokens)
    db = comp.storage._get_db()
    for tok in policy:
        db.execute(
            "UPDATE chunks SET origin=? WHERE id=?",
            (ORIGIN_CONSOLIDATION_POLICY, str(by_token[tok].id)),
        )
    db.commit()
    return parent, by_token


async def _hit(comp, query: str, window: int):
    results, _ = await comp.search_pipeline.search(query, context_window=window)
    hits = [r for r in results if query in r.chunk.content]
    assert len(hits) == 1, [r.chunk.content[:20] for r in results]
    return hits[0]


def _window_contents(hit) -> list[str]:
    ctx = hit.context
    return [c.content for c in (*ctx.window_before, *ctx.window_after)]


def _expand_header(out: str) -> str:
    return next(line for line in out.splitlines() if line.startswith("## Expand: chunk "))


class TestSearchSurface:
    @pytest.mark.parametrize("how", HIDES)
    async def test_a_policy_row_of_a_hidden_parent_is_not_context(self, bm25_only_components, how):
        comp, mem_dir = bm25_only_components
        parent, _ = await _consolidated(comp, mem_dir, ["tokKiwi", "tokZephyr"], {"tokKiwi"})
        await _hide(comp, parent, how)

        assert (await comp.search_pipeline.search("tokKiwi"))[0] == []
        hit = await _hit(comp, "tokZephyr", 2)

        assert not any("tokKiwi" in c for c in _window_contents(hit))
        assert (hit.context.chunk_position, hit.context.total_chunks_in_file) == (1, 1)

    @pytest.mark.parametrize("how", AVAILABILITY)
    async def test_a_hidden_row_shrinks_the_window_it_does_not_step_over(
        self, bm25_only_components, how
    ):
        """The physical window is chosen first, then the hidden row is dropped.

        Filtering before the seek's ``LIMIT`` would reach past the hidden
        middle row for the far one and present it as adjacent.
        """
        comp, mem_dir = bm25_only_components
        tokens = ["tokGap0", "tokGap1", "tokGap2"]
        parent, _ = await _consolidated(comp, mem_dir, tokens, {"tokGap1"})
        await _hide(comp, parent, how)

        first = await _hit(comp, "tokGap0", 1)
        last = await _hit(comp, "tokGap2", 1)

        assert _window_contents(first) == [] and _window_contents(last) == []
        assert (first.context.chunk_position, first.context.total_chunks_in_file) == (1, 2)
        assert (last.context.chunk_position, last.context.total_chunks_in_file) == (2, 2)

    @pytest.mark.parametrize("how", AVAILABILITY)
    async def test_a_hold_committed_after_retrieval_hides_the_neighbours(
        self, bm25_only_components, monkeypatch, how
    ):
        """The anchor's own file is hidden between retrieval and the window read.

        The anchor was retrieved while visible and is still returned; the
        neighbours and the counts are read under the hold and leave the rows
        out, the per-statement rule the redaction hold follows (#2777).
        """
        comp, mem_dir = bm25_only_components
        path = mem_dir / "win.md"
        await _index_sections(comp, path, ["tokWin0", "tokWin1", "tokWin2"])
        real = comp.storage.get_context_windows

        async def hide_then_read(*args, **kwargs):
            await _hide(comp, path, how)
            return await real(*args, **kwargs)

        monkeypatch.setattr(comp.storage, "get_context_windows", hide_then_read)

        hit = await _hit(comp, "tokWin1", 2)

        assert _window_contents(hit) == []
        assert (hit.context.chunk_position, hit.context.total_chunks_in_file) == (1, 1)


class TestIdSurface:
    @pytest.mark.parametrize("how", NOT_WITHHELD)
    async def test_mem_expand_still_shows_a_neighbour_not_held_for_its_content(
        self, bm25_only_components, how
    ):
        """Pinned on purpose: the id surface keeps the recovery read (ADR-0036)."""
        comp, mem_dir = bm25_only_components
        parent, by_token = await _consolidated(comp, mem_dir, ["tokKiwi", "tokZephyr"], {"tokKiwi"})
        await _hide(comp, parent, how)
        ctx = StubCtx(AppContext.from_components(comp))

        out = await search.mem_expand(chunk_id=str(by_token["tokZephyr"].id), ctx=ctx)

        assert "tokKiwi" in out
        assert _expand_header(out).startswith("## Expand: chunk 2/2 ")

    async def test_mem_expand_still_hides_a_redaction_held_neighbour(self, bm25_only_components):
        comp, mem_dir = bm25_only_components
        parent, by_token = await _consolidated(comp, mem_dir, ["tokKiwi", "tokZephyr"], {"tokKiwi"})
        await _hide(comp, parent, REDACTION_BLOCKED_HOLD)
        ctx = StubCtx(AppContext.from_components(comp))

        out = await search.mem_expand(chunk_id=str(by_token["tokZephyr"].id), ctx=ctx)

        assert "tokKiwi" not in out
        assert _expand_header(out).startswith("## Expand: chunk 1/1 ")
