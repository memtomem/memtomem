"""``mem_reflect_save`` links related chunks to the insight it wrote (#3273).

It used to take the newest row in the store as "the chunk just written", so a
write elsewhere that landed between the append and that lookup received the
``informs_reflection`` edges. ``new_chunk_ids`` is no substitute on its own: it
lists every chunk the re-index of ``reflections.md`` upserted. So the save
links only when the attribution is unambiguous and says so when it is not.

Every test asserts the edge's target id (or its absence) against a real store.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from uuid import uuid4

import pytest

from helpers import StubCtx
from memtomem.models import Chunk, ChunkMetadata
from memtomem.server.context import AppContext
from memtomem.server.tools import memory_crud, reflection

UNLINKED = reflection._UNLINKED_NOTICE


@pytest.fixture
async def stack(bm25_only_components):
    comp, mem_dir = bm25_only_components
    related_file = mem_dir / "related.md"
    related_file.write_text("## Related\n\nA chunk the insight draws on.\n", encoding="utf-8")
    await comp.index_engine.index_file(related_file, ingress_guarded=True)
    (related,) = await comp.storage.list_chunks_by_source(related_file)
    ctx = StubCtx(AppContext.from_components(comp))
    return comp, mem_dir, ctx, related


def _reflections(mem_dir: Path) -> Path:
    return mem_dir / "reflections.md"


async def _chunk_holding(comp, mem_dir: Path, token: str) -> Chunk:
    (chunk,) = [
        c
        for c in await comp.storage.list_chunks_by_source(_reflections(mem_dir))
        if token in c.content
    ]
    return chunk


async def _edges(comp, related: Chunk) -> list:
    return await comp.storage.get_related(related.id)


async def _save(ctx, insight: str, related: Chunk, **kwargs) -> str:
    result = await reflection.mem_reflect_save(
        insight=insight, related_chunks=[str(related.id)], ctx=ctx, **kwargs
    )
    assert result.startswith("Insight saved."), result
    return result


async def _seed(comp, mem_dir: Path, text: str, *, index: bool = True) -> None:
    path = _reflections(mem_dir)
    with open(path, "a", encoding="utf-8") as f:
        f.write(text)
    if index:
        await comp.index_engine.index_file(path, ingress_guarded=True)


# ---------------------------------------------------------------------------
# Linked
# ---------------------------------------------------------------------------


class TestLinksItsOwnChunk:
    @pytest.mark.asyncio
    async def test_a_save_links_the_chunk_holding_the_insight(self, stack):
        comp, mem_dir, ctx, related = stack
        result = await _save(ctx, "zqnormal insight about caching.", related, tags=["x"])

        insight = await _chunk_holding(comp, mem_dir, "zqnormal")
        assert await _edges(comp, related) == [(insight.id, "informs_reflection")]
        assert UNLINKED not in result

    @pytest.mark.asyncio
    async def test_a_write_elsewhere_in_the_gap_does_not_take_the_edge(self, stack, monkeypatch):
        # Lands after the insight's re-index and before the link step, in another
        # namespace and file. It is the newest row in the store.
        comp, mem_dir, ctx, related = stack
        intruder = Chunk(
            content="zqintruder written by another agent",
            metadata=ChunkMetadata(source_file=mem_dir / "agent.md", namespace="agent-runtime:w1"),
            content_hash=f"hash-{uuid4().hex}",
            embedding=[0.1] * 1024,
        )
        real = reflection._get_app_initialized

        async def write_then_get(ctx_):
            await comp.storage.upsert_chunks([intruder])
            return await real(ctx_)

        monkeypatch.setattr(reflection, "_get_app_initialized", write_then_get)
        await _save(ctx, "zqgap insight about retries.", related)

        insight = await _chunk_holding(comp, mem_dir, "zqgap")
        assert await _edges(comp, related) == [(insight.id, "informs_reflection")]

    @pytest.mark.asyncio
    async def test_a_short_entry_merged_into_the_previous_section_links_the_merge(self, stack):
        comp, mem_dir, ctx, related = stack
        await _save(ctx, "zqfirst short insight.", related)
        await _save(ctx, "zqsecond short insight.", related)

        merged = await _chunk_holding(comp, mem_dir, "zqsecond")
        assert "zqfirst" in merged.content
        # The first save's chunk was replaced by the merge, and its edge went
        # with it (relations follow chunk ids); that is not this fix's to change.
        assert await _edges(comp, related) == [(merged.id, "informs_reflection")]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("insight", "token"),
        [
            # The chunker rewrites each of these, so a hand-written comparison
            # of the insight text against the chunk would refuse them.
            ("Use [[Redis]] for the zqwiki cache layer.", "zqwiki"),
            ("# Topic zqheading\n\nbody text under the heading.", "body text under"),
            ("alpha zqlonecr\rbeta on the next line.", "zqlonecr"),
        ],
    )
    async def test_text_the_chunker_rewrites_is_still_linked(self, stack, insight, token):
        comp, mem_dir, ctx, related = stack
        result = await _save(ctx, insight, related)

        chunk = await _chunk_holding(comp, mem_dir, token)
        assert await _edges(comp, related) == [(chunk.id, "informs_reflection")], result


# ---------------------------------------------------------------------------
# Not linked, and the result says so
# ---------------------------------------------------------------------------


class TestUnsureLinksNothing:
    @pytest.mark.asyncio
    async def test_a_pending_edit_indexed_with_the_insight(self, stack, monkeypatch):
        comp, mem_dir, ctx, related = stack
        await _seed(comp, mem_dir, "## Seed\n\nseed body that is already indexed.\n")
        await _seed(
            comp, mem_dir, "\n## Earlier\n\nzqearlier body nobody indexed yet.\n", index=False
        )
        seen = []
        real = memory_crud._mem_add_core

        async def record(*args, **kwargs):
            out = await real(*args, **kwargs)
            seen.append(out[1])
            return out

        monkeypatch.setattr(memory_crud, "_mem_add_core", record)
        result = await _save(ctx, "zqpending insight body.", related)

        assert len(seen[0].new_chunk_ids) == 2
        assert await _edges(comp, related) == []
        assert result.endswith(f"\n- {UNLINKED}")

    @pytest.mark.asyncio
    async def test_a_heading_only_insight(self, stack):
        # The formatter puts the entry's metadata before a ``## `` heading, so
        # the previous section changes, and a bare heading yields no chunk.
        comp, mem_dir, ctx, related = stack
        await _seed(comp, mem_dir, "## Earlier\n\nzqearlier unrelated body.\n")
        result = await _save(ctx, "## Unique", related)

        assert await _edges(comp, related) == []
        assert result.endswith(f"\n- {UNLINKED}")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("ids", ["none", "unrelated", "insight-and-unrelated"])
    async def test_new_ids_that_do_not_single_out_the_insight(self, stack, monkeypatch, ids):
        comp, mem_dir, ctx, related = stack
        real = memory_crud._mem_add_core

        async def rewrite_ids(*args, **kwargs):
            result, stats = await real(*args, **kwargs)
            new_ids = {
                "none": (),
                "unrelated": (related.id,),
                "insight-and-unrelated": (*stats.new_chunk_ids, related.id),
            }[ids]
            return result, dataclasses.replace(stats, new_chunk_ids=new_ids)

        monkeypatch.setattr(memory_crud, "_mem_add_core", rewrite_ids)
        result = await _save(ctx, "zqstub insight body.", related)

        assert await _edges(comp, related) == []
        assert result.endswith(f"\n- {UNLINKED}")

    @pytest.mark.asyncio
    async def test_one_new_id_but_an_entry_of_several_chunks(self, stack, monkeypatch):
        # The entry would chunk into more than one piece: one new chunk cannot
        # be all of it, whatever it holds.
        comp, mem_dir, ctx, related = stack
        real = comp.index_engine._chunk_projected_content

        def two_pieces(path, text):
            chunks = real(path, text)
            return [*chunks, *chunks]

        monkeypatch.setattr(comp.index_engine, "_chunk_projected_content", two_pieces)
        result = await _save(ctx, "zqsplit insight body.", related)

        assert await _edges(comp, related) == []
        assert result.endswith(f"\n- {UNLINKED}")

    @pytest.mark.asyncio
    async def test_an_attribution_error_still_reports_the_save(self, stack, monkeypatch):
        # The insight is on disk and indexed by then; an error result would
        # invite a retry that appends it a second time.
        comp, mem_dir, ctx, related = stack

        real = reflection.resolve_chunk

        async def boom(app, chunk_id):
            # Only the attribution's read of the new chunk fails; the related
            # ids still resolve.
            if chunk_id != related.id:
                raise RuntimeError("store read failed")
            return await real(app, chunk_id)

        monkeypatch.setattr(reflection, "resolve_chunk", boom)
        result = await _save(ctx, "zqboom insight body.", related)

        assert result.endswith(f"\n- {UNLINKED}")
        assert await _edges(comp, related) == []
        assert _reflections(mem_dir).read_text(encoding="utf-8").count("zqboom") == 1


# ---------------------------------------------------------------------------
# The ``on_appended`` seam in ``_mem_add_core``
# ---------------------------------------------------------------------------


def _rows_holding(comp, token: str) -> int:
    db = comp.storage._get_db()
    return db.execute(
        "SELECT COUNT(*) FROM chunks WHERE content LIKE ?", (f"%{token}%",)
    ).fetchone()[0]


class TestOnAppended:
    @staticmethod
    async def _add(ctx, seen, **kwargs):
        return await memory_crud._mem_add_core(
            content=kwargs.pop("content", "zqseam entry body."),
            title="Seam",
            tags=None,
            file=kwargs.pop("file", "seam.md"),
            namespace=None,
            template=None,
            ctx=ctx,
            event_type="mem_add",
            on_appended=seen.append,
            **kwargs,
        )

    @pytest.mark.asyncio
    async def test_called_once_with_the_appended_text_before_the_re_index(self, stack):
        comp, mem_dir, ctx, _ = stack
        seen: list[str] = []
        at_call: list[tuple[str, int]] = []

        def record(text: str) -> None:
            seen.append(text)
            at_call.append(
                ((mem_dir / "seam.md").read_text(encoding="utf-8"), _rows_holding(comp, "zqseam"))
            )

        result, stats = await memory_crud._mem_add_core(
            content="zqseam entry body.",
            title="Seam",
            tags=None,
            file="seam.md",
            namespace=None,
            template=None,
            ctx=ctx,
            event_type="mem_add",
            on_appended=record,
        )

        assert stats is not None, result
        assert len(seen) == 1 and "zqseam entry body." in seen[0]
        on_disk, indexed_rows = at_call[0]
        assert on_disk.endswith(seen[0])
        assert indexed_rows == 0
        assert _rows_holding(comp, "zqseam") == 1

    @pytest.mark.asyncio
    async def test_not_called_on_an_idempotent_replay(self, stack):
        comp, mem_dir, ctx, _ = stack
        first: list[str] = []
        second: list[str] = []
        await self._add(ctx, first, idempotency_key="k-seam")
        _, stats = await self._add(ctx, second, idempotency_key="k-seam")

        assert len(first) == 1
        assert second == [] and stats is None

    @pytest.mark.asyncio
    async def test_not_called_when_the_write_is_refused(self, stack):
        comp, mem_dir, ctx, _ = stack
        comp.config.indexing.exclude_patterns = ["**/blocked.md"]
        seen: list[str] = []
        result, stats = await self._add(ctx, seen, file="blocked.md")

        assert stats is None, result
        assert seen == []
        assert not (mem_dir / "blocked.md").exists()


class TestContextDependentMerge:
    @pytest.mark.asyncio
    async def test_a_merge_that_adds_a_heading_inside_the_entry(self, stack, monkeypatch):
        # Merged with the section before it, the entry's parts get a heading
        # between them that the entry alone does not have, so the one new chunk
        # does not contain the entry's own projection.
        comp, mem_dir, ctx, related = stack
        await _seed(comp, mem_dir, "# Root\n\nseed body.\n")
        seen = []
        real = memory_crud._mem_add_core

        async def record(*args, **kwargs):
            out = await real(*args, **kwargs)
            seen.append(out[1])
            return out

        monkeypatch.setattr(memory_crud, "_mem_add_core", record)
        result = await _save(ctx, "zqintro line.\n\n### Child\n\nzqchild body.", related)

        assert len(seen[0].new_chunk_ids) == 1
        assert await _edges(comp, related) == []
        assert result.endswith(f"\n- {UNLINKED}")
