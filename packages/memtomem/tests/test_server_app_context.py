"""Tests for ``AppContext.ensure_initialized`` lock semantics + ownership (issue #399).

These cover the property/factory plumbing that lets handlers do lazy
initialization without race conditions on first call. Phase 3 also moved
watcher/scheduler/watchdog start into ``ensure_initialized``; the
``_no_background_loops`` autouse fixture below stubs those classes out
so unit tests focus on the lock + ownership logic without leaking real
watchdog threads / asyncio tasks across tests. The end-to-end lazy-init
behavior (handshake leaves DB absent, first tool call creates it) is
covered separately in ``tests/test_lazy_init_acceptance.py``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from memtomem.config import Mem2MemConfig
from memtomem.server.component_factory import Components, TeardownResult
from memtomem.server.context import AppContext


@pytest.fixture(autouse=True)
def _no_background_loops(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub ``FileWatcher`` so ``ensure_initialized`` doesn't spin a real
    watchdog Observer thread for every unit test in this file.

    All scheduler enabled-flags default to ``False`` (see ``config.py``),
    so ``ConsolidationScheduler`` / ``PolicyScheduler`` / ``HealthWatchdog``
    are not even constructed under default config — only ``FileWatcher``
    needs to be mocked.
    """
    from memtomem.indexing import watcher as watcher_mod

    def _make_fake_watcher(*_args: object, **_kwargs: object) -> object:
        fake = MagicMock()
        fake.start = AsyncMock()
        fake.stop = AsyncMock()
        return fake

    monkeypatch.setattr(watcher_mod, "FileWatcher", _make_fake_watcher)


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

    async def fake_close(comp: Components) -> TeardownResult:
        close_calls.append(comp)
        return TeardownResult(storage_closed=True)

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
        mock_close.return_value = TeardownResult(storage_closed=True)
        await ctx.close()

    mock_close.assert_called_once_with(fake_components)
    assert ctx._components is None
    assert ctx._owns_components is False


# ── Phase 3: background-loop ownership ────────────────────────────────


@pytest.mark.asyncio
async def test_ensure_initialized_starts_file_watcher_under_default_config(
    fake_components: Components,
) -> None:
    """Phase 3 moved ``FileWatcher.start`` from ``app_lifespan`` into
    ``ensure_initialized``. Default config has all schedulers disabled,
    so the watcher is the only thing that should start."""
    ctx = AppContext(config=fake_components.config)

    with patch(
        "memtomem.server.component_factory.create_components",
        return_value=fake_components,
    ):
        await ctx.ensure_initialized()

    assert ctx._watcher is not None, "watcher should be allocated and stashed on ctx"
    ctx._watcher.start.assert_awaited_once()  # type: ignore[attr-defined]
    # Schedulers/watchdog stay None because the default config has them disabled.
    assert ctx._scheduler is None
    assert ctx._policy_scheduler is None
    assert ctx._health_watchdog is None


@pytest.mark.asyncio
async def test_ensure_initialized_skips_watcher_in_degraded_mode(
    fake_components: Components,
) -> None:
    """When ``embedding_broken`` is set (issue #349 degraded mode), the
    watcher is constructed but ``start()`` is skipped — same gate as the
    pre-Phase-3 lifespan code, just relocated into ``ensure_initialized``.
    This stops the watcher from crashing on the missing ``chunks_vec``
    table; ``mem_embedding_reset`` recovers the install."""
    fake_components.embedding_broken = {"reason": "dim_mismatch"}
    ctx = AppContext(config=fake_components.config)

    with patch(
        "memtomem.server.component_factory.create_components",
        return_value=fake_components,
    ):
        await ctx.ensure_initialized()

    assert ctx._watcher is not None, "watcher allocated even in degraded mode"
    ctx._watcher.start.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_close_stops_started_watcher(fake_components: Components) -> None:
    """Phase 3 moved watcher ownership to ``AppContext``. ``close()`` must
    therefore stop the watcher it started — otherwise the watchdog
    Observer thread + asyncio processor task leak past lifespan
    teardown."""
    ctx = AppContext(config=fake_components.config)

    with patch(
        "memtomem.server.component_factory.create_components",
        return_value=fake_components,
    ):
        await ctx.ensure_initialized()

    started_watcher = ctx._watcher
    assert started_watcher is not None

    with patch("memtomem.server.component_factory.close_components") as mock_close:
        mock_close.return_value = TeardownResult(storage_closed=True)
        await ctx.close()

    started_watcher.stop.assert_awaited_once()  # type: ignore[attr-defined]
    assert ctx._watcher is None, "ctx must drop its watcher reference after close"


@pytest.mark.asyncio
async def test_ensure_initialized_rolls_back_components_when_watcher_start_fails(
    fake_components: Components,
) -> None:
    """A ``watcher.start()`` failure must close the freshly-built
    components and reset ``_components`` so the context isn't poisoned —
    otherwise the sqlite handle the factory just opened leaks and a
    retry hits a half-initialized state."""
    from memtomem.indexing import watcher as watcher_mod

    boom_watcher = MagicMock()
    boom_watcher.start = AsyncMock(side_effect=RuntimeError("watcher boom"))
    boom_watcher.stop = AsyncMock()

    ctx = AppContext(config=fake_components.config)
    close_calls: list[Components] = []

    async def fake_close(comp: Components) -> TeardownResult:
        close_calls.append(comp)
        return TeardownResult(storage_closed=True)

    with (
        patch(
            "memtomem.server.component_factory.create_components",
            return_value=fake_components,
        ),
        patch("memtomem.server.component_factory.close_components", side_effect=fake_close),
        patch.object(watcher_mod, "FileWatcher", lambda *a, **k: boom_watcher),
        pytest.raises(RuntimeError, match="watcher boom"),
    ):
        await ctx.ensure_initialized()

    # Watcher we constructed got the failed ``start()``; cleanup must
    # invoke ``stop()`` so the partially-initialized resource does not
    # leak its Observer thread.
    boom_watcher.stop.assert_awaited_once()
    # Components were torn down so the sqlite/embedder handles don't leak.
    assert close_calls == [fake_components]
    # And the context is back to a clean slate — a retry can re-init.
    assert ctx._components is None
    assert ctx._owns_components is False
    assert ctx._watcher is None


# ── #2181: recovery from degraded startup ─────────────────────────────


def _enable_all_services(config: Mem2MemConfig) -> None:
    """Turn on every background service ``recover_from_degraded`` can start."""
    config.consolidation_schedule.enabled = True
    config.policy.enabled = True
    config.health_watchdog.enabled = True


async def _init_degraded(fake_components: Components) -> AppContext:
    """A lifespan-owned context that started in degraded mode."""
    fake_components.embedding_broken = {"reason": "dim_mismatch"}
    ctx = AppContext(config=fake_components.config)
    with patch(
        "memtomem.server.component_factory.create_components",
        return_value=fake_components,
    ):
        await ctx.ensure_initialized()
    return ctx


@pytest.mark.asyncio
async def test_recover_from_degraded_starts_what_startup_skipped(
    fake_components: Components,
) -> None:
    """The whole point of #2181: after the embedding mismatch is repaired,
    the loops degraded startup suppressed come up in-process."""
    _enable_all_services(fake_components.config)
    ctx = await _init_degraded(fake_components)
    ctx._watcher.start.assert_not_awaited()  # type: ignore[attr-defined]

    with (
        patch("memtomem.server.scheduler.ConsolidationScheduler") as consolidation,
        patch("memtomem.server.scheduler.PolicyScheduler") as policy,
        patch("memtomem.server.health_watchdog.HealthWatchdog") as watchdog,
    ):
        for factory in (consolidation, policy, watchdog):
            factory.return_value.start = AsyncMock()
        await ctx.recover_from_degraded()

    ctx._watcher.start.assert_awaited_once()  # type: ignore[attr-defined]
    assert ctx._scheduler is consolidation.return_value
    assert ctx._policy_scheduler is policy.return_value
    assert ctx._health_watchdog is watchdog.return_value
    # The startup snapshot is a stale reading once recovery has run; leaving
    # it set keeps every status surface reporting a degraded server.
    assert ctx.embedding_broken is None


@pytest.mark.asyncio
async def test_recover_from_degraded_is_noop_on_a_healthy_context(
    fake_components: Components,
) -> None:
    """A reset on a server that started healthy must not re-start the
    watcher — ``FileWatcher.start`` builds a fresh Observer every call, so a
    second start leaks the first one's thread and processor task."""
    ctx = AppContext(config=fake_components.config)
    with patch(
        "memtomem.server.component_factory.create_components",
        return_value=fake_components,
    ):
        await ctx.ensure_initialized()
    watcher = ctx._watcher
    watcher.start.assert_awaited_once()  # type: ignore[attr-defined]

    await ctx.recover_from_degraded()

    watcher.start.assert_awaited_once()  # type: ignore[attr-defined]
    assert ctx._watcher is watcher


@pytest.mark.asyncio
async def test_recover_from_degraded_skips_unowned_contexts(
    fake_components: Components,
) -> None:
    """``from_components`` contexts (CLI, tests) deliberately run no
    background loops and do not close the components they were handed.
    Growing loops on one would hand the supplier tasks it never asked for
    and never tears down."""
    _enable_all_services(fake_components.config)
    fake_components.embedding_broken = {"reason": "dim_mismatch"}
    ctx = AppContext.from_components(fake_components)

    with (
        patch("memtomem.server.scheduler.ConsolidationScheduler") as consolidation,
        patch("memtomem.server.health_watchdog.HealthWatchdog") as watchdog,
    ):
        await ctx.recover_from_degraded()

    consolidation.assert_not_called()
    watchdog.assert_not_called()
    assert ctx._scheduler is None
    assert ctx._health_watchdog is None
    # The stale snapshot is still cleared — status honesty does not depend
    # on owning the loops.
    assert ctx.embedding_broken is None


@pytest.mark.asyncio
async def test_recover_from_degraded_retries_a_failed_service(
    fake_components: Components,
) -> None:
    """A start that fails must not consume the only recovery attempt.

    One "already recovered" flag would do exactly that: the failure would be
    permanent until restart, which is the state #2181 exists to end.
    """
    _enable_all_services(fake_components.config)
    ctx = await _init_degraded(fake_components)

    starts: list[object] = []

    def _make_watchdog(*_args: object, **_kwargs: object) -> MagicMock:
        instance = MagicMock()
        instance.start = AsyncMock(side_effect=RuntimeError("watchdog boom"))
        instance.stop = AsyncMock()
        starts.append(instance)
        return instance

    with (
        patch("memtomem.server.scheduler.ConsolidationScheduler") as consolidation,
        patch("memtomem.server.scheduler.PolicyScheduler") as policy,
        patch("memtomem.server.health_watchdog.HealthWatchdog", side_effect=_make_watchdog),
    ):
        for factory in (consolidation, policy):
            factory.return_value.start = AsyncMock()
        await ctx.recover_from_degraded()

        # The failure is logged, not raised — the reset that triggered this
        # already succeeded — and it does not take the other services down.
        assert ctx._health_watchdog is None
        assert ctx._scheduler is consolidation.return_value
        starts[0].stop.assert_awaited_once()

        # Second reset: the still-missing service is tried again, the ones
        # already running are not rebuilt.
        await ctx.recover_from_degraded()

    assert len(starts) == 2
    assert consolidation.call_count == 1
    ctx._watcher.start.assert_awaited_once()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_recovery_retains_a_service_whose_cleanup_also_failed(
    fake_components: Components,
) -> None:
    """When the start fails the caller drops its only reference so the handle
    stays retryable — but a stop that also failed may have left a live loop.
    Dropping it there would put it beyond the reach of ``close()`` forever."""
    _enable_all_services(fake_components.config)
    ctx = await _init_degraded(fake_components)

    doomed = MagicMock(name="scheduler")
    doomed.start = AsyncMock(side_effect=RuntimeError("start boom"))
    doomed.stop = AsyncMock(side_effect=RuntimeError("stop boom"))

    with (
        patch("memtomem.server.scheduler.ConsolidationScheduler", return_value=doomed),
        patch("memtomem.server.scheduler.PolicyScheduler") as policy,
        patch("memtomem.server.health_watchdog.HealthWatchdog") as watchdog,
    ):
        for factory in (policy, watchdog):
            factory.return_value.start = AsyncMock()
        await ctx.recover_from_degraded()

    # Retryable (handle left None) *and* still reachable for shutdown.
    assert ctx._scheduler is None
    assert [service for service, _ in ctx._failed_services] == [doomed]

    doomed.stop.reset_mock()
    with patch("memtomem.server.component_factory.close_components") as mock_close:
        mock_close.return_value = TeardownResult(storage_closed=True)
        await ctx.close()

    doomed.stop.assert_awaited_once()
    assert ctx._failed_services == []


@pytest.mark.asyncio
async def test_watcher_retry_is_refused_when_its_cleanup_failed(
    fake_components: Components,
) -> None:
    """The watcher instance is reused across retries and ``FileWatcher.stop``
    clears its observer/task handles only on the way out. If that stop failed,
    calling ``start()`` again would replace handles that are still live and
    leave nothing able to stop them — so the retry is refused instead."""
    ctx = await _init_degraded(fake_components)
    watcher = ctx._watcher
    watcher.start = AsyncMock(side_effect=RuntimeError("start boom"))  # type: ignore[union-attr]
    watcher.stop = AsyncMock(side_effect=RuntimeError("stop boom"))  # type: ignore[union-attr]

    await ctx.recover_from_degraded()
    assert ctx._watcher_cleanup_failed is True
    watcher.start.assert_awaited_once()  # type: ignore[union-attr]

    # A later reset must not start over the attempt we could not stop.
    watcher.start = AsyncMock()  # type: ignore[union-attr]
    await ctx.recover_from_degraded()
    watcher.start.assert_not_awaited()  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_recover_from_degraded_serializes_concurrent_callers(
    fake_components: Components,
) -> None:
    """``apply_current`` holds no lock, so two resets can land together.
    Without ``_init_lock`` both would see an unstarted watcher and start it
    twice, leaking an Observer thread."""
    _enable_all_services(fake_components.config)
    ctx = await _init_degraded(fake_components)

    released = asyncio.Event()
    starts = 0

    async def _blocking_start() -> None:
        nonlocal starts
        starts += 1
        await released.wait()

    ctx._watcher.start = AsyncMock(side_effect=_blocking_start)  # type: ignore[union-attr]

    with (
        patch("memtomem.server.scheduler.ConsolidationScheduler") as consolidation,
        patch("memtomem.server.scheduler.PolicyScheduler") as policy,
        patch("memtomem.server.health_watchdog.HealthWatchdog") as watchdog,
    ):
        for factory in (consolidation, policy, watchdog):
            factory.return_value.start = AsyncMock()
        first = asyncio.create_task(ctx.recover_from_degraded())
        second = asyncio.create_task(ctx.recover_from_degraded())
        # Let both tasks reach the lock; only one may be inside it.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert starts == 1
        released.set()
        await asyncio.gather(first, second)

    assert starts == 1
    assert consolidation.call_count == 1
    assert policy.call_count == 1
    assert watchdog.call_count == 1
