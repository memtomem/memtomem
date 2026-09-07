"""LangGraph BaseStore compatibility for the file-backed adapter."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("langgraph")

from langgraph.store.base import GetOp, PutOp, SearchOp

from memtomem import privacy
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


def _registered_tier(tmp_path, monkeypatch):
    """A ``project_shared`` tier the loaded config actually knows about.

    ``MemtomemBaseStore`` builds its own config, so the registration has to
    reach it through the environment rather than a fixture object.
    """
    tier = tmp_path / "proj" / ".memtomem" / "memories"
    tier.mkdir(parents=True)
    monkeypatch.setenv("MEMTOMEM_INDEXING__PROJECT_MEMORY_DIRS", f'["{tier.as_posix()}"]')
    return tier


def test_project_shared_root_requires_confirmation_even_when_scope_says_user(tmp_path, monkeypatch):
    """The gate used to read the caller's *declared* scope while ``root``
    went unvalidated, so declaring ``user`` and pointing ``root`` at the
    git-tracked tier cleared Gate B and put files there anyway."""
    tier = _registered_tier(tmp_path, monkeypatch)

    with pytest.raises(ValueError, match="confirm_project_shared"):
        MemtomemBaseStore(root=tier / "store", scope="user")


def test_project_shared_root_is_accepted_with_confirmation_and_scans_as_shared(
    tmp_path, monkeypatch
):
    """With consent the store constructs — and every ``put`` must scan as
    ``project_shared``, or ``force_unsafe`` stays open on a git-tracked
    destination.

    Asserting ``store.scope`` alone would pass even if ``_put`` stopped
    forwarding that scope to the guard, so the behaviour is exercised: a
    secret under ``force_unsafe=True`` must be *hard-refused* rather than
    bypassed, which only happens when the guard is told the tier.
    """
    tier = _registered_tier(tmp_path, monkeypatch)

    store = MemtomemBaseStore(
        root=tier / "store",
        scope="user",
        confirm_project_shared=True,
        force_unsafe=True,
        embedding=EmbeddingConfig(provider="none", dimension=0),
    )

    assert store.scope == "project_shared"

    # ``put`` raising is NOT the discriminator: ``_put``'s own check reads
    # ``self.scope`` too, so it would still raise with the guard told the
    # wrong tier. What separates the two is the *decision the guard
    # rendered* — ``blocked_project_shared`` (unbypassable) rather than
    # ``bypassed`` (force_unsafe honoured). Only the counter shows that.
    privacy.reset_for_tests()
    with pytest.raises(ValueError, match="privacy"):
        store.put(("users",), "secret", {"api_key": "sk-secret-value"})

    counters = privacy.snapshot()["by_tool"]["langgraph_basestore_put"]
    assert counters["blocked_project_shared"] == 1
    assert counters["bypassed"] == 0, "force_unsafe must not be honoured on the git-tracked tier"
    assert list(store.root.rglob("*.json")) == [], "nothing may land in the git-tracked tier"

    # The same call on a user-tier store *is* bypassable — without this the
    # assertions above would also hold for a store that refuses everything.
    privacy.reset_for_tests()
    plain = MemtomemBaseStore(
        root=tmp_path / "plain",
        force_unsafe=True,
        embedding=EmbeddingConfig(provider="none", dimension=0),
    )
    plain.put(("users",), "secret", {"api_key": "sk-secret-value"})
    plain_counters = privacy.snapshot()["by_tool"]["langgraph_basestore_put"]
    assert plain_counters["bypassed"] == 1
    assert plain_counters["blocked_project_shared"] == 0
    assert list(plain.root.rglob("*.json"))


def test_a_root_outside_any_project_tier_stays_user(tmp_path, monkeypatch):
    """Escalation only happens for a registered tier — an ordinary
    directory must not be promoted into needing consent."""
    _registered_tier(tmp_path, monkeypatch)

    store = MemtomemBaseStore(
        root=tmp_path / "plain", embedding=EmbeddingConfig(provider="none", dimension=0)
    )

    assert store.scope == "user"


def test_declared_project_shared_survives_an_unregistered_root(tmp_path, monkeypatch):
    """Never downgrade. A caller who declares the shared tier keeps the
    stricter Gate A even when the path does not classify — inferring
    ``user`` from an unregistered tree would reopen ``force_unsafe``."""
    monkeypatch.delenv("MEMTOMEM_INDEXING__PROJECT_MEMORY_DIRS", raising=False)

    store = MemtomemBaseStore(
        root=tmp_path / "unregistered",
        scope="project_shared",
        confirm_project_shared=True,
        embedding=EmbeddingConfig(provider="none", dimension=0),
    )

    assert store.scope == "project_shared"


def test_explicit_root_does_not_inherit_persisted_embedding_config(tmp_path, monkeypatch):
    """The tier lookup must not drag ambient configuration in with it.

    An explicit-``root`` store saw ``EmbeddingConfig`` defaults before
    #2321, and classifying the root is no reason for it to start resolving
    a persisted — possibly remote — provider instead.
    """
    home = tmp_path / "home"
    (home / ".memtomem").mkdir(parents=True)
    (home / ".memtomem" / "config.json").write_text(
        json.dumps({"embedding": {"provider": "openai", "model": "text-embedding-3-small"}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    store = MemtomemBaseStore(root=tmp_path / "explicit")

    assert store._embedding_config.provider != "openai"


def test_classifying_an_explicit_root_does_not_rewrite_global_config(tmp_path, monkeypatch):
    """``load_config_overrides`` migrates by default, so a lookup done for a
    *refusal* could rewrite the user's config as a side effect of
    constructing a store that then raises."""
    home = tmp_path / "home"
    (home / ".memtomem").mkdir(parents=True)
    config_path = home / ".memtomem" / "config.json"
    config_path.write_text(json.dumps({"indexing": {"auto_discover": True}}), encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    before = config_path.read_text(encoding="utf-8")

    MemtomemBaseStore(
        root=tmp_path / "explicit", embedding=EmbeddingConfig(provider="none", dimension=0)
    )

    assert config_path.read_text(encoding="utf-8") == before


def test_ttl_is_explicitly_unsupported(store):
    with pytest.raises(NotImplementedError, match="TTL"):
        store.put(("users",), "ttl", {"x": 1}, ttl=10)
