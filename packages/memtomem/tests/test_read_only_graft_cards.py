"""Graft's structural cards index as a read-only root and stay out of the write path.

Pins the recipe measured on 2026-09-22 (memtomem-docs
``planning/graft-read-only-root-recipe-2026-09-22.md``): a ``graft build``
tree is plain markdown cards (heading + symbol bullets, no front matter) next
to ``.graph/wiring.json`` and ``.cache/*.json``. The recipe registers the tree
under ``indexing.read_only_memory_dirs`` with two exclude patterns. What has
to hold for the recipe to be honest:

- the cards index and search; the JSON never does (the size guard is what
  rejected the real 32 MB ``wiring.json`` — a small one would otherwise be
  chunked leaf by leaf, #2481);
- every card chunk is stamped ``source_read_only`` so the per-chunk gates
  refuse writes without re-reading the configuration;
- a rebuild that touches one card re-indexes that card only — the sibling's
  chunk ids and content hashes are untouched, which is what makes
  ``graft build`` on every commit affordable.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from helpers import StubCtx
from memtomem.server.context import AppContext
from memtomem.server.tools import memory_crud

CARD_A = (
    "# packages/memtomem/src/memtomem/search/fusion.py\n\n"
    "- weighted_rank_scores · function · L11-L21 — def weighted_rank_scores[T](result_lists, weights, k: int = 60)\n"
    "- reciprocal_rank_fusion · function · L24-L102 — def reciprocal_rank_fusion(result_lists, k: int = 60, top_k: int = 10)\n"
)
CARD_B = (
    "# packages/memtomem/src/memtomem/search/mmr.py\n\n"
    "- maximal_marginal_relevance · function · L9-L60 — def maximal_marginal_relevance(results, lambda_: float = 0.7)\n"
)
WIRING = '{"meta": {"version": 1, "nodeCount": 2}, "nodes": [{"id": "a", "kind": "function"}]}\n'


def _graft_tree(root: Path) -> tuple[Path, Path, Path, Path]:
    cards = root / "packages" / "memtomem" / "src" / "memtomem" / "search"
    cards.mkdir(parents=True)
    a = cards / "fusion.md"
    b = cards / "mmr.md"
    a.write_text(CARD_A, encoding="utf-8")
    b.write_text(CARD_B, encoding="utf-8")
    (root / ".graph").mkdir()
    wiring = root / ".graph" / "wiring.json"
    wiring.write_text(WIRING, encoding="utf-8")
    (root / ".cache").mkdir()
    cache = root / ".cache" / "extract.json"
    cache.write_text('{"k": 1}\n', encoding="utf-8")
    return a, b, wiring, cache


@pytest.fixture
async def graft_root(bm25_only_components, tmp_path):
    comp, _mem_dir = bm25_only_components
    root = tmp_path / "graft"
    root.mkdir()
    a, b, wiring, cache = _graft_tree(root)
    indexing = comp.config.indexing
    indexing.read_only_memory_dirs = [root]
    indexing.exclude_patterns = ["**/.graph/**", "**/.cache/**"]
    stats = await comp.index_engine.index_path(root, recursive=True)
    assert not stats.errors
    return comp, root, a, b, wiring, cache


async def test_cards_index_and_json_does_not(graft_root):
    comp, root, a, b, wiring, cache = graft_root
    assert await comp.storage.list_chunks_by_source(a)
    assert await comp.storage.list_chunks_by_source(b)
    assert not await comp.storage.list_chunks_by_source(wiring)
    assert not await comp.storage.list_chunks_by_source(cache)
    assert comp.index_engine.is_excluded(wiring)
    assert comp.index_engine.is_excluded(cache)


async def test_every_card_chunk_is_stamped_read_only(graft_root):
    comp, root, a, b, *_ = graft_root
    for source in (a, b):
        chunks = await comp.storage.list_chunks_by_source(source)
        assert chunks
        assert all(c.metadata.source_read_only for c in chunks)


async def test_a_card_is_searchable_by_symbol_name(graft_root):
    comp, root, a, *_ = graft_root
    results, _ = await comp.search_pipeline.search("reciprocal_rank_fusion", top_k=3)
    assert results
    assert results[0].chunk.metadata.source_file == a


async def test_rebuilding_one_card_leaves_the_sibling_untouched(graft_root):
    comp, root, a, b, *_ = graft_root
    before_b = [(c.id, c.content_hash) for c in await comp.storage.list_chunks_by_source(b)]
    before_a = [(c.id, c.content_hash) for c in await comp.storage.list_chunks_by_source(a)]
    # graft build rewrote fusion.py's card (a new symbol) and left mmr.py's card byte-identical
    a.write_text(
        CARD_A + "- _source_label · function · L85-L91 — def _source_label(cid)\n", encoding="utf-8"
    )
    stats = await comp.index_engine.index_path(root, recursive=True)
    assert not stats.errors
    after_b = [(c.id, c.content_hash) for c in await comp.storage.list_chunks_by_source(b)]
    after_a = [(c.id, c.content_hash) for c in await comp.storage.list_chunks_by_source(a)]
    assert after_b == before_b
    assert after_a != before_a
    # Ids and hashes are deterministic, so a rebuild that re-upserted every card
    # (``force=True``) would still pass the two lines above. The counters are what
    # show the sibling was skipped rather than rewritten with identical values.
    assert stats.skipped_chunks == len(before_b)
    assert stats.indexed_chunks == len(after_a)


async def test_a_memory_write_under_the_graft_root_is_refused(graft_root):
    """The cards are searchable, but ``mem_add`` may not land a note among them."""
    comp, root, *_ = graft_root
    cards = root / "packages" / "memtomem" / "src" / "memtomem" / "search"
    before = sorted(p.name for p in cards.iterdir())
    ctx = StubCtx(AppContext.from_components(comp))

    result = await memory_crud.mem_add("some content", file=str(cards / "note.md"), ctx=ctx)

    # A read-only root is not a writable root, so the earlier "outside configured
    # memory directories" gate answers first; the read-only gate is the backstop
    # for the case where someone also lists the tree as writable (refused at
    # startup by ``check_read_only_roots_disjoint``). This test pins the refusal
    # and the untouched directory, not which gate gave it; the read-only gate
    # itself is pinned by ``test_mem_add_refuses_without_leaving_a_lock_sidecar``
    # in ``test_read_only_root_mutation.py``.
    assert "read_only_target" in result or "outside configured memory directories" in result, result
    assert sorted(p.name for p in cards.iterdir()) == before
