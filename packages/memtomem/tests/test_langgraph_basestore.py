"""LangGraph BaseStore compatibility for the file-backed adapter."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("langgraph")

from langgraph.store.base import GetOp, PutOp, SearchOp

from memtomem.config import EmbeddingConfig
from memtomem.integrations.langgraph import MemtomemBaseStore


@pytest.fixture
def store(tmp_path):
    instance = MemtomemBaseStore(
        root=tmp_path / "store",
        embedding=EmbeddingConfig(provider="none", dimension=0),
    )
    yield instance
    instance.close()


def test_sync_crud_preserves_created_at_and_file_truth(store):
    store.put(("users", "alice"), "prefs", {"food": "pizza"})
    first = store.get(("users", "alice"), "prefs")
    assert first and first.value == {"food": "pizza"}
    store.put(("users", "alice"), "prefs", {"food": "pasta"})
    second = store.get(("users", "alice"), "prefs")
    assert second and second.created_at == first.created_at
    assert second.updated_at >= first.updated_at
    records = list(store.root.rglob("*.json"))
    assert len(records) == 1
    assert json.loads(records[0].read_text())["value"] == {"food": "pasta"}
    store.delete(("users", "alice"), "prefs")
    assert store.get(("users", "alice"), "prefs") is None


def test_search_skips_corrupt_records(store):
    store.put(("users", "alice"), "good", {"food": "pizza"})
    corrupt = store.root / "corrupt.json"
    corrupt.write_text("{not-json", encoding="utf-8")
    results = store.search(("users",), query="pizza")
    assert [result.key for result in results] == ["good"]


@pytest.mark.asyncio
async def test_async_batch_prefix_filter_and_pagination(store):
    await store.abatch(
        [
            PutOp(("users", "a"), "one", {"kind": "note", "rank": 1}),
            PutOp(("users", "a"), "two", {"kind": "note", "rank": 2}),
            PutOp(("users", "b"), "three", {"kind": "other", "rank": 3}),
        ]
    )
    result = await store.asearch(("users",), filter={"kind": "note", "rank": {"$gte": 2}}, limit=1)
    assert [item.key for item in result] == ["two"]
    assert await store.alist_namespaces(prefix=("users",), max_depth=2) == [
        ("users", "a"),
        ("users", "b"),
    ]
    batch = await store.abatch([GetOp(("users", "a"), "one"), SearchOp(("users", "a"), limit=1)])
    assert batch[0].key == "one"
    assert len(batch[1]) == 1


@pytest.mark.asyncio
async def test_semantic_query_falls_back_to_lexical_on_minimal_install(store):
    await store.aput(("docs",), "python", {"text": "Python async task groups"})
    await store.aput(("docs",), "garden", {"text": "Tomato garden watering"})
    hits = await store.asearch(("docs",), query="Python async", limit=2)
    assert hits[0].key == "python"
    assert hits[0].score > hits[1].score


def test_index_false_excludes_item_from_semantic_projection(store):
    store.put(("docs",), "hidden", {"text": "unique needle"}, index=False)
    store.put(("docs",), "visible", {"text": "ordinary text"})
    hits = store.search(("docs",), query="unique needle", limit=2)
    assert hits[0].key != "hidden"
    assert store.get(("docs",), "hidden") is not None


def test_privacy_and_project_shared_gates(tmp_path):
    with pytest.raises(ValueError, match="confirm_project_shared"):
        MemtomemBaseStore(root=tmp_path / "blocked", scope="project_shared")
    store = MemtomemBaseStore(
        root=tmp_path / "safe", embedding=EmbeddingConfig(provider="none", dimension=0)
    )
    with pytest.raises(ValueError, match="privacy"):
        store.put(("users",), "secret", {"api_key": "sk-secret-value"})


def test_ttl_is_explicitly_unsupported(store):
    with pytest.raises(NotImplementedError, match="TTL"):
        store.put(("users",), "ttl", {"x": 1}, ttl=10)
