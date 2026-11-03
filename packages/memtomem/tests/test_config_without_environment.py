"""``Mem2MemConfig.without_environment``: the given values, validated, no shell.

Measured before this existed (pydantic-settings 2.16.2): ``Mem2MemConfig()``
and ``Mem2MemConfig.model_validate({})`` both read ``MEMTOMEM_*``;
``model_construct`` reads nothing but also validates nothing.
"""

from __future__ import annotations

import pickle

import pytest

from memtomem.config import ExplicitMem2MemConfig, Mem2MemConfig


def test_default_constructor_reads_the_environment_but_without_environment_does_not(
    monkeypatch,
) -> None:
    monkeypatch.setenv("MEMTOMEM_SEARCH__CACHE_TTL", "7")
    monkeypatch.setenv("MEMTOMEM_STORAGE__SQLITE_PATH", "/nowhere/env.db")

    ambient = Mem2MemConfig()
    assert ambient.search.cache_ttl == 7.0
    assert str(ambient.storage.sqlite_path) == "/nowhere/env.db"

    explicit = Mem2MemConfig.without_environment()
    assert explicit.search.cache_ttl == 30.0
    assert str(explicit.storage.sqlite_path) != "/nowhere/env.db"


def test_without_environment_takes_the_given_values(monkeypatch) -> None:
    monkeypatch.setenv("MEMTOMEM_SEARCH__CACHE_TTL", "7")
    cfg = Mem2MemConfig.without_environment(search={"cache_ttl": 3.5})
    assert cfg.search.cache_ttl == 3.5


def test_without_environment_still_validates() -> None:
    with pytest.raises(ValueError, match="default_top_k must be positive"):
        Mem2MemConfig.without_environment(search={"default_top_k": 0})


def test_without_environment_is_a_mem2memconfig_and_pickles() -> None:
    cfg = Mem2MemConfig.without_environment(search={"cache_ttl": 3.5})
    assert isinstance(cfg, Mem2MemConfig)
    assert type(cfg) is ExplicitMem2MemConfig
    assert "ExplicitMem2MemConfig" in repr(cfg)
    clone = pickle.loads(pickle.dumps(cfg))
    assert clone.search.cache_ttl == 3.5


def test_langfuse_keys_are_outside_the_guarantee(monkeypatch) -> None:
    # The Langfuse validator reads LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY
    # from os.environ directly, in every constructor. without_environment
    # skips the MEMTOMEM_* settings sources and nothing else, so enabling
    # tracing without explicit keys fails here exactly as it does ambiently.
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    with pytest.raises(ValueError, match="LANGFUSE"):
        Mem2MemConfig.without_environment(session_trace={"enabled": True, "langfuse_enabled": True})
