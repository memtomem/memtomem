"""Pinned Context storage, shadowing, budgets, and composition."""

from __future__ import annotations

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
        metadata=SimpleNamespace(source_file="memory.md"),
    )
    result = SimpleNamespace(chunk=chunk, score=0.9)

    class Pipeline:
        async def search(self, **kwargs):
            assert kwargs["query"] == "deployment"
            return [result], None

    bundle = await ContextAssembler(pinned_store, Pipeline()).compose("deployment")
    assert bundle.pinned[0].content == "always visible"
    assert bundle.retrieved[0]["content"] == "retrieved memory"
    assert bundle.used_chars == len("always visible") + len("retrieved memory")


def test_delete_is_exact_and_confirmed_for_shared(pinned_store):
    pinned_store.set("one", "content")
    assert pinned_store.delete("one") is True
    assert pinned_store.delete("one") is False
