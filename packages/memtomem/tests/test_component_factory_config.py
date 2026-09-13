"""Configuration precedence tests for the component factory."""

import pytest


@pytest.mark.asyncio
async def test_factory_can_skip_already_resolved_ambient_config(monkeypatch):
    import memtomem.config as _cfg
    import memtomem.runtime.components as _factory

    def _unexpected(_config):
        raise AssertionError("ambient configuration was loaded again")

    monkeypatch.setattr(_cfg, "load_config_d", _unexpected)
    monkeypatch.setattr(_cfg, "load_config_overrides", _unexpected)

    config = _cfg.Mem2MemConfig()
    config.storage.sqlite_path = ":memory:"

    # Reaching create_storage proves the ambient loaders were skipped. Abort
    # there to keep this unit test hermetic.
    monkeypatch.setattr(
        _factory,
        "create_storage",
        lambda _config: (_ for _ in ()).throw(RuntimeError("factory reached")),
    )
    with pytest.raises(RuntimeError, match="factory reached"):
        await _factory.create_components(config, load_ambient_config=False)


@pytest.mark.asyncio
async def test_factory_cleanup_attempts_every_resource_and_preserves_init_error(monkeypatch):
    import memtomem.config as _cfg
    import memtomem.runtime.components as _factory

    closed: list[str] = []

    class BrokenStorage:
        async def initialize(self):
            raise RuntimeError("initialization root cause")

        async def close(self):
            closed.append("storage")
            raise RuntimeError("storage cleanup failed")

    class BrokenEmbedder:
        async def close(self):
            closed.append("embedder")
            raise RuntimeError("embedder cleanup failed")

    monkeypatch.setattr(_factory, "create_storage", lambda _config: BrokenStorage())
    monkeypatch.setattr(_factory, "create_embedder", lambda _config: BrokenEmbedder())
    config = _cfg.Mem2MemConfig()

    with pytest.raises(RuntimeError, match="initialization root cause"):
        await _factory.create_components(config, load_ambient_config=False)

    assert closed == ["embedder", "storage"]


@pytest.mark.parametrize("failing", ["IndexEngine", "SearchPipeline", "Components"])
async def test_factory_shared_pipeline_failure_closes_each_resource_once(monkeypatch, failing):
    from unittest.mock import AsyncMock, MagicMock

    import memtomem.config as config_module
    import memtomem.runtime.components as factory

    config = config_module.Mem2MemConfig()
    config.rerank.enabled = True
    config.llm.enabled = True
    storage = MagicMock(embedding_mismatch=None)
    storage.initialize = AsyncMock()
    storage.close = AsyncMock()
    embedder = MagicMock()
    embedder.close = AsyncMock()
    llm = MagicMock()
    llm.close = AsyncMock()
    reranker = MagicMock()
    # Whether owned by the caller or a completed pipeline, a failing close
    # must still release the remaining resources and retain the root cause.
    reranker.close = AsyncMock(side_effect=RuntimeError("reranker cleanup failed"))
    monkeypatch.setattr(factory, "create_storage", lambda _config: storage)
    monkeypatch.setattr(factory, "create_embedder", lambda _config: embedder)
    monkeypatch.setattr("memtomem.llm.factory.create_llm", lambda _config: llm)
    monkeypatch.setattr(
        "memtomem.search.reranker.factory.create_reranker", lambda _config: reranker
    )
    error = RuntimeError("construction failed")
    monkeypatch.setattr(factory, failing, MagicMock(side_effect=error))

    with pytest.raises(RuntimeError) as caught:
        await factory.create_components(config, load_ambient_config=False, entity_backfill=False)

    assert caught.value is error
    for resource in (reranker, llm, embedder, storage):
        resource.close.assert_awaited_once()
