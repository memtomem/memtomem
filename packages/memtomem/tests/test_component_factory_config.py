"""Configuration precedence tests for the component factory."""

import pytest


@pytest.mark.asyncio
async def test_factory_can_skip_already_resolved_ambient_config(monkeypatch):
    import memtomem.config as _cfg
    import memtomem.server.component_factory as _factory

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
