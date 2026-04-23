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
    """Uses ``RuntimeError`` not ``AssertionError`` so the check survives
    ``python -O`` / ``PYTHONOPTIMIZE=1`` — pre-init access is a real
    programming bug we want to surface even when asserts are stripped."""
    ctx = AppContext(config=Mem2MemConfig())
    with pytest.raises(RuntimeError, match="ensure_initialized"):
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


# ── post-factory failure / ownership coverage ──────────────────────────


@pytest.mark.asyncio
async def test_ensure_initialized_closes_components_if_post_factory_step_raises(
    fake_components: Components,
) -> None:
    """A failure in DedupScanner construction must not leak the sqlite /
    embedder handles the factory already opened. ``close_components`` is
    called before re-raising; the context stays uninitialized so a retry
    is still possible."""
    ctx = AppContext(config=fake_components.config)
    close_calls: list[Components] = []

    async def fake_close(comp: Components) -> None:
        close_calls.append(comp)

    def exploding_dedup(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("post-factory boom")

    with (
        patch(
            "memtomem.server.component_factory.create_components",
            return_value=fake_components,
        ),
        patch("memtomem.server.component_factory.close_components", side_effect=fake_close),
        patch("memtomem.search.dedup.DedupScanner", side_effect=exploding_dedup),
    ):
        with pytest.raises(RuntimeError, match="post-factory boom"):
            await ctx.ensure_initialized()

    assert close_calls == [fake_components], (
        "Post-factory failure must trigger close_components so sqlite / embedder "
        "handles opened by create_components don't leak."
    )
    # Context is clean: neither _components nor _dedup_scanner stays populated,
    # and _owns_components didn't flip on.
    assert ctx._components is None
    assert ctx.dedup_scanner is None
    assert ctx._owns_components is False


@pytest.mark.asyncio
async def test_close_after_from_components_does_not_touch_caller_owned(
    fake_components: Components,
) -> None:
    """from_components → close must not invoke close_components: the
    caller (cli_components / test fixture) retains ownership and will
    close the supplied Components themselves. Calling close_components
    here would leave the caller with already-torn-down handles."""
    ctx = AppContext.from_components(fake_components)

    with patch("memtomem.server.component_factory.close_components") as mock_close:
        await ctx.close()

    mock_close.assert_not_called()
    # Context still drops its view of the components so accidental
    # post-close access fails loudly — the caller owns the lifecycle,
    # but the context stops handing out its storage/embedder.
    assert ctx._components is None
    assert ctx._owns_components is False
    assert ctx.dedup_scanner is None


@pytest.mark.asyncio
async def test_close_after_ensure_initialized_closes_components(
    fake_components: Components,
) -> None:
    """ensure_initialized → close must tear the Components down — we
    built them, so it's our job to close them. Mirrors the from_components
    test above in inverse."""
    ctx = AppContext(config=fake_components.config)

    with patch(
        "memtomem.server.component_factory.create_components",
        return_value=fake_components,
    ):
        await ctx.ensure_initialized()

    assert ctx._owns_components is True

    with patch("memtomem.server.component_factory.close_components") as mock_close:
        await ctx.close()

    mock_close.assert_called_once_with(fake_components)
    assert ctx._components is None
    assert ctx._owns_components is False


def test_set_health_watchdog_exposes_via_property() -> None:
    """``ctx.set_health_watchdog(wd)`` is the lifespan seam — reader side
    is the ``health_watchdog`` property, not ``_health_watchdog`` poking."""
    ctx = AppContext(config=Mem2MemConfig())
    sentinel = object()

    ctx.set_health_watchdog(sentinel)

    assert ctx.health_watchdog is sentinel
