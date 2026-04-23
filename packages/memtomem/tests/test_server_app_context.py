"""Tests for ``AppContext.ensure_initialized`` lock semantics (issue #399, Phase 1).

These cover the property/factory plumbing that lets the lifespan keep eager
initialization today and lets handlers move to lazy initialization in
Phase 2/3 without race conditions on first call.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from memtomem.config import Mem2MemConfig
from memtomem.server.component_factory import Components
from memtomem.server.context import AppContext


@pytest.fixture
def fake_components() -> Components:
    """A bare ``Components`` stand-in for the parts ``ensure_initialized`` reads.

    Storage / embedder are sentinel objects — ``ensure_initialized`` only
    constructs the ``DedupScanner`` over them, and the dedup-scanner itself
    just stores the references; nothing calls into them in these tests.
    """
    return Components(
        config=Mem2MemConfig(),
        storage=object(),  # type: ignore[arg-type]
        embedder=object(),  # type: ignore[arg-type]
        index_engine=object(),  # type: ignore[arg-type]
        search_pipeline=object(),  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_ensure_initialized_concurrent_calls_invoke_factory_once(
    fake_components: Components,
) -> None:
    """Three coroutines hitting a fresh context simultaneously result in one init."""
    ctx = AppContext(config=fake_components.config)
    call_count = 0

    async def slow_create(_config: Mem2MemConfig) -> Components:
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.01)
        return fake_components

    with patch("memtomem.server.component_factory.create_components", side_effect=slow_create):
        results = await asyncio.gather(
            ctx.ensure_initialized(),
            ctx.ensure_initialized(),
            ctx.ensure_initialized(),
        )

    assert call_count == 1
    assert results[0] is results[1] is results[2] is fake_components
    assert ctx._components is fake_components
    assert ctx.dedup_scanner is not None


@pytest.mark.asyncio
async def test_ensure_initialized_idempotent(fake_components: Components) -> None:
    """Subsequent calls return the cached components without re-invoking the factory."""
    ctx = AppContext(config=fake_components.config)

    with patch(
        "memtomem.server.component_factory.create_components",
        return_value=fake_components,
    ) as mock_create:
        first = await ctx.ensure_initialized()
        second = await ctx.ensure_initialized()

    assert mock_create.call_count == 1
    assert first is second is fake_components


@pytest.mark.asyncio
async def test_ensure_initialized_failure_releases_lock_for_retry(
    fake_components: Components,
) -> None:
    """A transient failure leaves the context retryable rather than poisoned."""
    ctx = AppContext(config=fake_components.config)
    attempt = 0

    async def flaky_create(_config: Mem2MemConfig) -> Components:
        nonlocal attempt
        attempt += 1
        if attempt == 1:
            raise RuntimeError("transient init failure")
        return fake_components

    with patch("memtomem.server.component_factory.create_components", side_effect=flaky_create):
        with pytest.raises(RuntimeError, match="transient init failure"):
            await ctx.ensure_initialized()
        # Lock released, retry succeeds.
        comp = await ctx.ensure_initialized()

    assert comp is fake_components
    assert attempt == 2


@pytest.mark.asyncio
async def test_from_components_skips_factory(fake_components: Components) -> None:
    """``ensure_initialized`` returns the pre-supplied components without calling the factory."""
    ctx = AppContext.from_components(fake_components)

    with patch("memtomem.server.component_factory.create_components") as mock_create:
        comp = await ctx.ensure_initialized()

    mock_create.assert_not_called()
    assert comp is fake_components
    assert ctx.dedup_scanner is not None


def test_storage_access_before_init_raises() -> None:
    ctx = AppContext(config=Mem2MemConfig())
    with pytest.raises(AssertionError, match="ensure_initialized"):
        _ = ctx.storage


def test_embedding_broken_before_init_returns_none() -> None:
    """Mirrors the old field default — None until init runs."""
    ctx = AppContext(config=Mem2MemConfig())
    assert ctx.embedding_broken is None


def test_llm_provider_before_init_returns_none() -> None:
    """Optional even after init — None when components absent matches old field."""
    ctx = AppContext(config=Mem2MemConfig())
    assert ctx.llm_provider is None


def test_dedup_scanner_before_init_returns_none() -> None:
    ctx = AppContext(config=Mem2MemConfig())
    assert ctx.dedup_scanner is None


def test_health_watchdog_before_init_returns_none() -> None:
    ctx = AppContext(config=Mem2MemConfig())
    assert ctx.health_watchdog is None
