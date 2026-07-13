"""Pinned Context storage, shadowing, budgets, and composition."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from memtomem.config import Mem2MemConfig
from memtomem.pinned import ContextAssembler, PinnedContextStore


@pytest.fixture
def pinned_store(tmp_path):
    config = Mem2MemConfig()
    config.indexing.memory_dirs = [tmp_path / "user-memory"]
    project = tmp_path / "project"
    project.mkdir()
    return PinnedContextStore(config, project_root=project)


def test_file_round_trip_and_scope_shadow(pinned_store):
    pinned_store.set("style", "user style", scope="user", priority=1)
    pinned_store.set(
        "style",
        "team style",
        scope="project_shared",
        confirm_project_shared=True,
        priority=2,
    )
    pinned_store.set("style", "local style", scope="project_local", priority=3)
    effective = pinned_store.list()
    assert len(effective) == 1
    assert effective[0].content == "local style"
    assert effective[0].scope == "project_local"
    assert pinned_store.get("style", scope="user").content == "user style"


def test_agent_specific_block_shadows_general_even_from_lower_scope(pinned_store):
    pinned_store.set("rules", "team general", scope="project_shared", confirm_project_shared=True)
    pinned_store.set("rules", "planner rules", scope="user", agent_id="planner")
    assert pinned_store.list(agent_id="planner")[0].content == "planner rules"
    assert pinned_store.list(agent_id="worker")[0].content == "team general"


def test_search_exclusion_roots_cover_every_scope_without_reading_files(pinned_store):
    roots = pinned_store.search_exclusion_roots()
    assert roots == (
        pinned_store._base("user").resolve(),
        pinned_store._base("project_shared").resolve(),
        pinned_store._base("project_local").resolve(),
    )


def test_privacy_size_and_project_confirmation_gates(pinned_store):
    with pytest.raises(ValueError, match="exceeds"):
        pinned_store.set("large", "x" * 2001)
    with pytest.raises(ValueError, match="confirmation"):
        pinned_store.set("team", "safe", scope="project_shared")
    with pytest.raises(ValueError, match="privacy"):
        pinned_store.set("secret", "api_key=sk-secret")


@pytest.mark.asyncio
async def test_compose_never_splits_blocks_and_reports_omissions(pinned_store):
    pinned_store.set("first", "a" * 1500, priority=2)
    pinned_store.set("second", "b" * 1500, priority=1)
    bundle = await ContextAssembler(pinned_store).compose(max_chars=2000)
    assert [block.block_id for block in bundle.pinned] == ["first"]
    assert bundle.omitted_block_ids == ("second",)
    assert bundle.used_chars == 1500


@pytest.mark.asyncio
async def test_compose_pinned_first_then_retrieval(pinned_store):
    pinned_store.set("profile", "always visible", priority=1)
    chunk = SimpleNamespace(
        id="chunk-1",
        content="retrieved memory",
        metadata=SimpleNamespace(source_file="memory.md", namespace="work"),
    )
    result = SimpleNamespace(chunk=chunk, score=0.9)

    class Pipeline:
        async def search(self, **kwargs):
            assert kwargs["query"] == "deployment"
            assert kwargs["namespace"] == ["work", "shared"]
            assert kwargs["context_window"] == 2
            assert kwargs["exclude_source_roots"] == pinned_store.search_exclusion_roots()
            return [result], None

    bundle = await ContextAssembler(pinned_store, Pipeline()).compose(
        "deployment", namespace=["work", "shared"], context_window=2
    )
    assert bundle.pinned[0].content == "always visible"
    assert bundle.retrieved[0]["content"] == "retrieved memory"
    assert bundle.retrieved[0]["namespace"] == "work"
    assert bundle.used_chars == len("always visible") + len("retrieved memory")


@pytest.mark.asyncio
async def test_compose_schema_three_budgets_neighbors_and_preserves_source_order(pinned_store):
    def adjacent(chunk_id: str, content: str):
        return SimpleNamespace(
            id=chunk_id,
            content=content,
            metadata=SimpleNamespace(source_file="memory.md", namespace="work"),
        )

    before_far = adjacent("before-far", "BFAR")
    before_near = adjacent("before-near", "BN")
    hit = adjacent("hit", "HIT")
    after_near = adjacent("after-near", "AN")
    after_far = adjacent("after-far", "AFAR")
    result = SimpleNamespace(
        chunk=hit,
        score=0.9,
        context=SimpleNamespace(
            window_before=(before_far, before_near),
            window_after=(after_near, after_far),
            chunk_position=3,
            total_chunks_in_file=5,
        ),
    )

    class Pipeline:
        async def search(self, **kwargs):
            return [result], None

    bundle = await ContextAssembler(pinned_store, Pipeline()).compose(
        "deployment", max_chars=11, context_window=2
    )

    retrieved = bundle.retrieved[0]
    assert retrieved["content"] == "HIT"
    assert [item["id"] for item in retrieved["context"]["before"]] == [
        "before-far",
        "before-near",
    ]
    assert [item["id"] for item in retrieved["context"]["after"]] == ["after-near"]
    assert retrieved["context"]["chunk_position"] == 3
    assert retrieved["context"]["total_chunks_in_file"] == 5
    assert bundle.used_chars == 11


@pytest.mark.asyncio
async def test_compose_schema_three_keeps_hit_when_neighbors_exceed_budget(pinned_store):
    neighbor = SimpleNamespace(
        id="before",
        content="TOO-LARGE",
        metadata=SimpleNamespace(source_file="memory.md", namespace="work"),
    )
    hit = SimpleNamespace(
        id="hit",
        content="HIT",
        metadata=SimpleNamespace(source_file="memory.md", namespace="work"),
    )
    result = SimpleNamespace(
        chunk=hit,
        score=0.9,
        context=SimpleNamespace(
            window_before=(neighbor,),
            window_after=(),
            chunk_position=2,
            total_chunks_in_file=2,
        ),
    )

    class Pipeline:
        async def search(self, **kwargs):
            return [result], None

    bundle = await ContextAssembler(pinned_store, Pipeline()).compose(
        "deployment", max_chars=3, context_window=1
    )

    assert bundle.retrieved == (
        {
            "id": "hit",
            "content": "HIT",
            "source": "memory.md",
            "namespace": "work",
            "score": 0.9,
        },
    )
    assert bundle.used_chars == 3


@pytest.mark.asyncio
async def test_compose_schema_three_preserves_hits_and_deduplicates_context(pinned_store):
    def chunk(chunk_id: str, content: str):
        return SimpleNamespace(
            id=chunk_id,
            content=content,
            metadata=SimpleNamespace(source_file="memory.md", namespace="work"),
        )

    first = chunk("first", "AAAA")
    second = chunk("second", "BBBB")
    shared = chunk("shared", "CC")
    results = [
        SimpleNamespace(
            chunk=first,
            score=0.9,
            context=SimpleNamespace(
                window_before=(),
                window_after=(second, shared),
                chunk_position=0,
                total_chunks_in_file=3,
            ),
        ),
        SimpleNamespace(
            chunk=second,
            score=0.8,
            context=SimpleNamespace(
                window_before=(first,),
                window_after=(shared,),
                chunk_position=1,
                total_chunks_in_file=3,
            ),
        ),
    ]

    class Pipeline:
        async def search(self, **kwargs):
            return results, None

    bundle = await ContextAssembler(pinned_store, Pipeline()).compose(
        "deployment", max_chars=10, context_window=2
    )

    assert [item["id"] for item in bundle.retrieved] == ["first", "second"]
    assert "context" not in bundle.retrieved[0]
    assert [item["id"] for item in bundle.retrieved[1]["context"]["after"]] == ["shared"]
    assert bundle.used_chars == 10


@pytest.mark.asyncio
async def test_mem_context_compose_tool_threads_schema_three_scope(monkeypatch, pinned_store):
    from memtomem.server.tools import pinned as pinned_tools

    pinned_store.set("profile", "always visible", priority=1)
    chunk = SimpleNamespace(
        id="chunk-1",
        content="retrieved memory",
        metadata=SimpleNamespace(source_file="memory.md", namespace="work"),
    )
    result = SimpleNamespace(chunk=chunk, score=0.9)

    class Pipeline:
        async def search(self, **kwargs):
            assert kwargs["namespace"] == "work"
            assert kwargs["context_window"] == 1
            assert kwargs["exclude_source_roots"] == pinned_store.search_exclusion_roots()
            return [result], None

    app = SimpleNamespace(search_pipeline=Pipeline())

    async def fake_store(ctx):
        return app, pinned_store

    monkeypatch.setattr(pinned_tools, "_store", fake_store)
    payload = json.loads(
        await pinned_tools.mem_context_compose(
            query="deployment",
            namespace="work",
            context_window=1,
        )
    )

    assert payload["pinned"][0]["content"] == "always visible"
    assert payload["retrieved"][0]["namespace"] == "work"


def test_delete_is_exact_and_confirmed_for_shared(pinned_store):
    pinned_store.set("one", "content")
    assert pinned_store.delete("one") is True
    assert pinned_store.delete("one") is False
