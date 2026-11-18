"""Behaviour against LangGraph's reference ``InMemoryStore`` (ADR-0042, acceptance).

There is no conformance suite, so the reference store is the contract. The
reference has no embeddings here, so a query returns every item unscored;
every fixture item therefore contains the query word, and the comparison is
on the set of keys, not on order or score. Ordering
and scores are this store's own and are tested in ``test_search.py``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from langgraph.store.base import BaseStore
from langgraph.store.memory import InMemoryStore

from langchain_memtomem import MemtomemPipelineStore

Scenario = Callable[[BaseStore], Awaitable[Any]]

ITEMS: list[tuple[tuple[str, ...], str, dict[str, Any]]] = [
    (("users",), "root-item", {"text": "apples at the root", "n": 0}),
    (("users", "alice"), "a1", {"text": "alice likes apples", "n": 1, "meta": {"kind": "fruit"}}),
    (
        ("users", "alice"),
        "a2",
        {"text": "alice rides trains to the apples market", "n": 2, "meta": {"kind": "travel"}},
    ),
    (("users", "alice", "private"), "p1", {"text": "apples in the private garden", "n": 3}),
    (("Users",), "case", {"text": "apples uppercase sibling", "n": 4}),
    (("users", "bob"), "b1", {"text": "bob bakes apples into a pie", "n": 5}),
]


async def _fill(store: BaseStore) -> None:
    for namespace, key, value in ITEMS:
        await store.aput(namespace, key, value)


def _keys(items: list[Any]) -> set[str]:
    return {item.key for item in items}


async def _both(root: Path, scenario: Scenario) -> tuple[Any, Any]:
    reference = InMemoryStore()
    await _fill(reference)
    expected = await scenario(reference)
    ours = await MemtomemPipelineStore.open(root)
    try:
        await _fill(ours)
        actual = await scenario(ours)
    finally:
        await ours.aclose()
    return expected, actual


async def test_get_round_trip(root: Path) -> None:
    async def scenario(store: BaseStore) -> Any:
        item = await store.aget(("users", "alice"), "a1")
        assert item is not None
        missing = await store.aget(("users", "alice"), "nope")
        return item.value, item.namespace, item.key, missing

    expected, actual = await _both(root, scenario)
    assert expected == actual


async def test_put_replaces_and_delete_removes(root: Path) -> None:
    async def scenario(store: BaseStore) -> Any:
        await store.aput(("users", "alice"), "a1", {"text": "alice now likes pears", "n": 9})
        replaced = (await store.aget(("users", "alice"), "a1")).value  # type: ignore[union-attr]
        await store.adelete(("users", "alice"), "a2")
        gone = await store.aget(("users", "alice"), "a2")
        await store.adelete(("users", "alice"), "never-existed")
        return replaced, gone, _keys(await store.asearch(("users", "alice"), limit=50))

    expected, actual = await _both(root, scenario)
    assert expected == actual


@pytest.mark.parametrize(
    "prefix",
    [("users",), ("users", "alice"), ("users", "alice", "private"), ("Users",), ("nobody",), ()],
)
async def test_search_prefix_invariant_with_and_without_query(
    root: Path, prefix: tuple[str, ...]
) -> None:
    async def scenario(store: BaseStore) -> Any:
        plain = _keys(await store.asearch(prefix, limit=50))
        queried = _keys(await store.asearch(prefix, query="apples", limit=50))
        return plain, queried

    expected, actual = await _both(root, scenario)
    assert expected == actual


@pytest.mark.parametrize(
    "filters",
    [
        {"n": 1},
        {"n": {"$gt": 2}},
        {"n": {"$gte": 2, "$lt": 5}},
        {"n": {"$ne": 1}},
        {"n": {"$eq": 2}},
        {"meta": {"kind": "fruit"}},
        {"text": "no such text"},
    ],
)
async def test_filter_semantics_shared_with_the_reference(
    root: Path, filters: dict[str, Any]
) -> None:
    """The filter shapes both stores read the same way."""

    async def scenario(store: BaseStore) -> Any:
        plain = _keys(await store.asearch(("users",), filter=filters, limit=50))
        queried = _keys(await store.asearch(("users",), query="apples", filter=filters, limit=50))
        return plain, queried

    expected, actual = await _both(root, scenario)
    assert expected == actual


@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        ({"n": {"$in": [1, 3]}}, {"a1", "p1"}),
        ({"n": {"$nin": [1, 3]}}, {"root-item", "a2", "b1"}),
        ({"meta.kind": "fruit"}, {"a1"}),
        ({"meta.kind": {"$eq": "travel"}}, {"a2"}),
    ],
)
async def test_filter_extensions_of_the_record_store(
    root: Path, filters: dict[str, Any], expected: set[str]
) -> None:
    """``MemtomemBaseStore``'s filter, which this store applies on both paths.

    It accepts ``$in``/``$nin`` (the reference raises ``ValueError``) and
    dotted paths (the reference looks up the literal key). A nested mapping is
    compared for equality, where the reference matches a subset: that is the
    record store's contract, not this package's.
    """
    store = await MemtomemPipelineStore.open(root)
    try:
        await _fill(store)
        plain = _keys(await store.asearch(("users",), filter=filters, limit=50))
        queried = _keys(await store.asearch(("users",), query="apples", filter=filters, limit=50))
    finally:
        await store.aclose()
    assert plain == expected
    assert queried == expected


async def test_offset_and_limit_page_the_same_universe(root: Path) -> None:
    async def scenario(store: BaseStore) -> Any:
        pages = [
            _keys(await store.asearch(("users",), query="apples", limit=2, offset=offset))
            for offset in (0, 2, 4, 6)
        ]
        assert all(len(page) <= 2 for page in pages)
        assert not any(pages[i] & pages[j] for i in range(4) for j in range(i + 1, 4))
        return set().union(*pages)

    expected, actual = await _both(root, scenario)
    assert expected == actual


@pytest.mark.parametrize(
    "kwargs",
    [
        {"prefix": ("users",)},
        {"prefix": ("users", "alice")},
        {"suffix": ("alice",)},
        {"suffix": ("private",)},
        {"prefix": ("users", "*")},
        {"max_depth": 1},
        {"max_depth": 2},
        {"limit": 2},
        {"limit": 2, "offset": 2},
    ],
)
async def test_list_namespaces(root: Path, kwargs: dict[str, Any]) -> None:
    async def scenario(store: BaseStore) -> Any:
        return sorted(await store.alist_namespaces(**kwargs))

    expected, actual = await _both(root, scenario)
    assert expected == actual


async def test_index_false_items_are_stored_but_not_query_searchable(
    store: MemtomemPipelineStore,
) -> None:
    await store.aput(("users",), "visible", {"text": "apples visible"})
    await store.aput(("users",), "hidden", {"text": "apples hidden"}, index=False)
    assert (await store.aget(("users",), "hidden")).value == {"text": "apples hidden"}  # type: ignore[union-attr]
    assert _keys(await store.asearch(("users",), limit=10)) == {"visible", "hidden"}
    assert _keys(await store.asearch(("users",), query="apples", limit=10)) == {"visible"}
    # switching an item to index=False withdraws it from query search
    await store.aput(("users",), "visible", {"text": "apples visible"}, index=False)
    assert _keys(await store.asearch(("users",), query="apples", limit=10)) == set()
    # and back
    await store.aput(("users",), "hidden", {"text": "apples hidden"})
    assert _keys(await store.asearch(("users",), query="apples", limit=10)) == {"hidden"}


async def test_index_selectors_choose_the_searchable_fields(store: MemtomemPipelineStore) -> None:
    await store.aput(("docs",), "d1", {"title": "apples", "body": "bananas"}, index=["title"])
    assert _keys(await store.asearch(("docs",), query="apples", limit=10)) == {"d1"}
    assert _keys(await store.asearch(("docs",), query="bananas", limit=10)) == set()


async def test_put_with_none_value_deletes(store: MemtomemPipelineStore) -> None:
    from langgraph.store.base import PutOp

    await store.aput(("users",), "k", {"text": "apples"})
    await store.abatch([PutOp(("users",), "k", None)])
    assert await store.aget(("users",), "k") is None
    assert await store.asearch(("users",), query="apples", limit=10) == []


async def test_search_items_carry_a_score_and_timestamps(store: MemtomemPipelineStore) -> None:
    await store.aput(("users",), "k", {"text": "apples"})
    [item] = await store.asearch(("users",), query="apples", limit=10)
    assert item.score is not None and item.score > 0
    assert item.created_at <= item.updated_at
    [plain] = await store.asearch(("users",), limit=10)
    assert plain.score is None


async def test_sync_surface_outside_a_loop(root: Path) -> None:
    import asyncio

    def run() -> list[str]:
        store = asyncio.run(MemtomemPipelineStore.open(root))
        try:
            store.put(("users",), "k", {"text": "apples"})
            return [item.key for item in store.search(("users",), query="apples")]
        finally:
            store.close()

    assert await asyncio.to_thread(run) == ["k"]


async def test_sync_surface_inside_a_loop_is_refused(store: MemtomemPipelineStore) -> None:
    with pytest.raises(RuntimeError, match="abatch"):
        store.put(("users",), "k", {"text": "apples"})
