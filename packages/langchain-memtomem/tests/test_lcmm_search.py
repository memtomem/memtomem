"""Query search: namespace selection, item pagination, scores, the recall bound."""

from __future__ import annotations

from pathlib import Path

import pytest

from langchain_memtomem import MemtomemPipelineStore
from langchain_memtomem._protocol import to_memtomem_namespace


async def _keys(store: MemtomemPipelineStore, prefix: tuple[str, ...], **kwargs) -> list[str]:
    return [item.key for item in await store.asearch(prefix, query="apples", **kwargs)]


async def test_base64_prefix_collision_does_not_leak(store: MemtomemPipelineStore) -> None:
    # ``abc`` encodes to ``YWJj``; ``abcdef`` to ``YWJjZGVm`` — a LIKE prefix
    # over-matches, the decode-then-filter step must not.
    assert to_memtomem_namespace(("abcdef",)).startswith(to_memtomem_namespace(("abc",)))
    await store.aput(("abc",), "short", {"text": "apples short"})
    await store.aput(("abcdef",), "long", {"text": "apples long"})
    assert await _keys(store, ("abc",)) == ["short"]
    assert await _keys(store, ("abcdef",)) == ["long"]
    assert set(await _keys(store, ())) == {"short", "long"}


async def test_ascii_case_folded_sibling_does_not_leak(store: MemtomemPipelineStore) -> None:
    # ``aaa`` → ``YWFh`` and ``aaG`` → ``YWFH``: equal under LIKE's ASCII folding.
    assert to_memtomem_namespace(("aaa",)).lower() == to_memtomem_namespace(("aaG",)).lower()
    await store.aput(("aaa",), "lower", {"text": "apples lower"})
    await store.aput(("aaG",), "upper", {"text": "apples upper"})
    assert await _keys(store, ("aaa",)) == ["lower"]
    assert await _keys(store, ("aaG",)) == ["upper"]


async def test_multi_chunk_items_appear_once_with_their_best_score(
    store: MemtomemPipelineStore, long_text
) -> None:
    for n in range(4):
        await store.aput(("docs",), f"d{n}", {"text": long_text("apples")})
    hits = await store.asearch(("docs",), query="apples", limit=10)
    keys = [item.key for item in hits]
    assert sorted(keys) == ["d0", "d1", "d2", "d3"]
    assert len(keys) == len(set(keys))
    # the item's score is the best of its chunks: never below any single hit
    comp = store._comp
    assert comp is not None
    raw, _ = await comp.search_pipeline.search("apples", top_k=50, scope=store.scope)
    by_item: dict[str, float] = {}
    for result in raw:
        key = Path(result.chunk.metadata.source_file).stem
        by_item[key] = max(by_item.get(key, 0.0), result.score)
    for item in hits:
        from memtomem.integrations.langgraph_store import encode_segment

        assert item.score == pytest.approx(by_item[encode_segment(item.key)])


async def test_pages_are_over_items_not_chunks(store: MemtomemPipelineStore, long_text) -> None:
    for n in range(6):
        await store.aput(("docs",), f"d{n}", {"text": long_text("apples")})
    full = await _keys(store, ("docs",), limit=6)
    assert len(full) == 6
    pages = [await _keys(store, ("docs",), limit=2, offset=offset) for offset in (0, 2, 4)]
    assert [k for page in pages for k in page] == full
    assert await _keys(store, ("docs",), limit=2, offset=6) == []


@pytest.mark.parametrize("budget", [1, 3, 4, 7, 20, 33])
async def test_recall_is_bounded_by_max_chunks_per_search(root: Path, budget: int) -> None:
    store = await MemtomemPipelineStore.open(root, max_chunks_per_search=budget)
    try:
        for n in range(25):
            await store.aput(("docs",), f"d{n:02d}", {"text": f"apples note {n}"})
        hits = await _keys(store, ("docs",), limit=30)
        assert 0 < len(hits) <= min(budget, 25)
        assert await _keys(store, ("docs",), limit=2, offset=budget) == []
    finally:
        await store.aclose()


async def test_budget_below_one_is_refused(root: Path) -> None:
    with pytest.raises(ValueError):
        MemtomemPipelineStore(root, max_chunks_per_search=0)


async def test_widening_reaches_items_behind_many_chunks_of_others(root: Path, long_text) -> None:
    store = await MemtomemPipelineStore.open(root, max_chunks_per_search=200)
    try:
        for n in range(3):
            await store.aput(("docs",), f"big{n}", {"text": long_text("apples", sections=8)})
        await store.aput(("docs",), "small", {"text": "apples once"})
        hits = await _keys(store, ("docs",), limit=10)
        assert set(hits) == {"big0", "big1", "big2", "small"}
    finally:
        await store.aclose()


async def test_limit_zero_returns_nothing(store: MemtomemPipelineStore) -> None:
    await store.aput(("docs",), "d", {"text": "apples"})
    assert await store.asearch(("docs",), query="apples", limit=0) == []


async def test_query_search_sees_a_write_at_once(store: MemtomemPipelineStore) -> None:
    await store.aput(("docs",), "d", {"text": "apples"})
    assert await _keys(store, ("docs",)) == ["d"]
    await store.aput(("docs",), "d", {"text": "pears"})
    assert await _keys(store, ("docs",)) == []
    await store.adelete(("docs",), "d")
    assert await store.asearch(("docs",), query="pears", limit=10) == []
