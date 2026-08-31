"""A file's path retrieves it only when nothing's prose matches (#2224).

``chunks_fts`` is ``fts5(content, source_file)`` and an unqualified ``MATCH``
searches both columns. ``source_file`` is stored raw, so unicode61 splits it
into components and every directory and filename word became a searchable term:
on a real 6,995-chunk store, 71 of the top 100 hits for ``plans`` were chunks
about something else, surfaced because they live under ``.claude/plans/``, and
for auto-generated plan-filename words the path noise was the *entire* result
set.

**Why weighting the column 0.0 is not the fix.** It was the obvious one — the
issue proposes it — and it is not enough: RRF fuses on a result's *ordinal
rank* (``search/fusion.py``: ``w / (k + rank)``), not on its score, so a
path-only row scored 0.0 still occupies a BM25 slot and still contributes
positive fused relevance to the hybrid pipeline every caller actually uses. The
row has to not be *retrieved*, which is why the MATCH is column-qualified
instead. ``TestHybridPipeline`` below is the test that distinguishes the two:
the leg-level ones pass under either design.

The fallback is deliberate. Typing a filename into search is an undocumented
but plausible habit, and it still works — the path phase runs only once the
content phase has found nothing, so a path can never outrank prose.
"""

from __future__ import annotations

import pytest

from helpers import make_chunk as _make_chunk

#: Long filler so the content match has a *low* per-term density. fts5
#: normalises each column by its average length, so a short path carrying the
#: term outscores a long body mentioning it once — which is why the real
#: store's worst cases were long notes losing to plan filenames. A fixture with
#: short bodies ranks correctly even unfixed and proves nothing.
_FILLER = " ".join(f"word{i}" for i in range(200))


class TestBm25Leg:
    @pytest.mark.asyncio
    async def test_a_path_only_match_is_not_retrieved_when_content_matches(self, storage):
        content_hit = _make_chunk(
            f"{_FILLER} a note about octopus behaviour and habitats. {_FILLER}",
            source="notes/marine-biology.md",
        )
        path_only = [_make_chunk("short", source=f".claude/plans/octopus-{i}.md") for i in range(5)]
        await storage.upsert_chunks([*path_only, content_hit])

        results = await storage.bm25_search("octopus", top_k=10)

        assert [r.chunk.id for r in results] == [content_hit.id], (
            "a path-only chunk was retrieved alongside the chunk that says the word: "
            f"{[str(r.chunk.metadata.source_file) for r in results]}"
        )

    @pytest.mark.asyncio
    async def test_a_path_only_match_is_retrieved_when_nothing_else_matches(self, storage):
        """The fallback: filename lookup still works, it just cannot compete."""
        path_only = _make_chunk("body text with no marker word", source="reports/octopus.md")
        await storage.upsert_chunks([path_only])

        results = await storage.bm25_search("octopus", top_k=5)

        assert [r.chunk.id for r in results] == [path_only.id], (
            "the path stopped being searchable at all — the fallback is gone"
        )

    @pytest.mark.asyncio
    async def test_the_or_fallback_survives_inside_the_content_phase(self, storage):
        """A multi-term query with no AND hit still degrades to OR — on content."""
        content_hit = _make_chunk("a note about pangolin diets", source="notes/a.md")
        path_only = _make_chunk("short", source="plans/pangolin-and-quokka.md")
        await storage.upsert_chunks([path_only, content_hit])

        # "quokka" appears nowhere in content, so the AND pass finds nothing and
        # the OR pass answers — still without the path row.
        results = await storage.bm25_search("pangolin quokka", top_k=10)

        assert [r.chunk.id for r in results] == [content_hit.id], (
            f"OR fallback leaked the path row: "
            f"{[str(r.chunk.metadata.source_file) for r in results]}"
        )

    @pytest.mark.asyncio
    async def test_the_path_phase_has_its_own_or_fallback(self, storage):
        """Both phases run the same AND-then-OR ladder."""
        path_only = _make_chunk("nothing relevant here", source="plans/wombat-notes.md")
        await storage.upsert_chunks([path_only])

        results = await storage.bm25_search("wombat quokka", top_k=5)

        assert [r.chunk.id for r in results] == [path_only.id], (
            "the path phase did not fall back to OR for a multi-term query"
        )

    @pytest.mark.asyncio
    async def test_every_content_match_is_returned_before_any_path_phase_runs(self, storage):
        """One weak content match still suppresses the whole path phase."""
        weak = _make_chunk(f"{_FILLER} pangolin {_FILLER}", source="notes/long.md")
        paths = [_make_chunk("short", source=f"plans/pangolin-{i}.md") for i in range(3)]
        await storage.upsert_chunks([*paths, weak])

        results = await storage.bm25_search("pangolin", top_k=10)

        assert [r.chunk.id for r in results] == [weak.id]


class TestHybridPipeline:
    """The end-to-end shape — and the one that fails under 0.0-weighting.

    Zero-weighting demotes a path-only row inside the BM25 leg but still
    returns it, and RRF scores it by ordinal rank, so it reaches the fused
    results anyway. Only not retrieving it keeps it out.
    """

    @pytest.mark.asyncio
    async def test_a_path_only_chunk_does_not_surface_in_fused_results(self, pipeline, storage):
        content_hit = _make_chunk(
            f"{_FILLER} the octopus section discusses octopus habitats. {_FILLER}",
            source="notes/marine-biology.md",
        )
        path_only = [_make_chunk("short", source=f".claude/plans/octopus-{i}.md") for i in range(5)]
        await storage.upsert_chunks([*path_only, content_hit])

        results, _stats = await pipeline.search("octopus", top_k=10, record=False)

        surfaced = {str(r.chunk.metadata.source_file) for r in results}
        assert not any(".claude/plans/octopus-" in s for s in surfaced), (
            f"a path-only chunk reached the fused results: {sorted(surfaced)}"
        )
        assert any("marine-biology" in s for s in surfaced), (
            "the genuine content match did not survive the pipeline"
        )
