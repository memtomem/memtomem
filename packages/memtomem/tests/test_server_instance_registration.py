"""AppContext ↔ instance-registry wiring (#1935).

Covers the server-only opt-in flag, publication at storage init,
close-before-cleanup ordering (sentinel released only on a *confirmed*
storage close), rollback release, and cancellation accumulate-and-defer.
Registry lock semantics themselves are covered cross-process in
``test_instance_registry.py``; here the registry runs for real but inside
the per-test isolated runtime dir (conftest ``_isolated_instance_registry``).
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import memtomem
import memtomem._instance_registry as reg
from memtomem.config import Mem2MemConfig
from memtomem.server.component_factory import Components
from memtomem.server.context import AppContext


@pytest.fixture(autouse=True)
def _no_background_loops(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub ``FileWatcher`` (see ``test_server_app_context.py`` for why)."""
    from memtomem.indexing import watcher as watcher_mod

    def _make_fake_watcher(*_args: object, **_kwargs: object) -> object:
        fake = MagicMock()
        fake.start = AsyncMock()
        fake.stop = AsyncMock()
        return fake

    monkeypatch.setattr(watcher_mod, "FileWatcher", _make_fake_watcher)


@pytest.fixture
def db(tmp_path) -> Path:
    p = tmp_path / "server-store.db"
    p.write_bytes(b"sqlite-fake")
    return p


@pytest.fixture
def components(db) -> Components:
    config = Mem2MemConfig(storage={"sqlite_path": str(db)})
    return Components(
        config=config,
        storage=object(),  # type: ignore[arg-type]
        embedder=object(),  # type: ignore[arg-type]
        index_engine=object(),  # type: ignore[arg-type]
        search_pipeline=object(),  # type: ignore[arg-type]
    )


def _sentinels() -> list[Path]:
    d = reg.instances_dir()
    if not d.exists():
        return []
    return sorted(d.iterdir())


async def _init_flagged(components: Components) -> AppContext:
    ctx = AppContext(config=components.config, register_server_instance=True)
    with patch("memtomem.server.component_factory.create_components", return_value=components):
        await ctx.ensure_initialized()
    return ctx


class TestOptIn:
    def test_flag_is_set_only_by_the_lifespan(self) -> None:
        """The guard makes the scope: sweep the whole package for the flag
        so a future call site can't opt into registration unnoticed."""
        src = Path(memtomem.__file__).parent
        hits = sorted(
            p.relative_to(src).as_posix()
            for p in src.rglob("*.py")
            if "register_server_instance=True" in p.read_text(encoding="utf-8")
        )
        assert hits == ["server/lifespan.py"]

    @pytest.mark.asyncio
    async def test_unflagged_context_never_registers(self, components) -> None:
        ctx = AppContext(config=components.config)
        with patch("memtomem.server.component_factory.create_components", return_value=components):
            await ctx.ensure_initialized()
        assert _sentinels() == []
        await ctx.close()

    @pytest.mark.asyncio
    async def test_from_components_context_never_registers(self, components) -> None:
        ctx = AppContext.from_components(components)
        assert _sentinels() == []
        await ctx.close()
        assert _sentinels() == []


class TestPublicationAndRelease:
    @pytest.mark.asyncio
    async def test_flagged_context_publishes_with_store_digest(self, components, db) -> None:
        ctx = await _init_flagged(components)
        try:
            (sentinel,) = _sentinels()
            info = reg._parse_entry(sentinel)
            assert info is not None
            assert info.digest == reg.store_digest_for(db)
            # the sentinel is live: the context's own process holds it
            result = reg.enumerate_live_instances(info.digest)
            assert [i.path for i in result.instances] == [sentinel]
        finally:
            await ctx.close()

    @pytest.mark.asyncio
    async def test_normal_close_releases_sentinel(self, components) -> None:
        ctx = await _init_flagged(components)
        assert len(_sentinels()) == 1
        await ctx.close()
        assert _sentinels() == []
        assert ctx._instance_registration is None

    @pytest.mark.asyncio
    async def test_rollback_after_post_storage_failure_releases_sentinel(
        self, components, monkeypatch
    ) -> None:
        from memtomem.indexing import watcher as watcher_mod

        published: list[int] = []
        real_register = reg.register_instance

        def spying_register(path):
            inst = real_register(path)
            published.append(len(_sentinels()))
            return inst

        monkeypatch.setattr(reg, "register_instance", spying_register)

        def _broken_watcher(*_a: object, **_k: object) -> object:
            fake = MagicMock()
            fake.start = AsyncMock(side_effect=RuntimeError("watcher exploded"))
            fake.stop = AsyncMock()
            return fake

        monkeypatch.setattr(watcher_mod, "FileWatcher", _broken_watcher)
        ctx = AppContext(config=components.config, register_server_instance=True)
        with patch("memtomem.server.component_factory.create_components", return_value=components):
            with pytest.raises(RuntimeError, match="watcher exploded"):
                await ctx.ensure_initialized()
        assert published == [1]  # the sentinel really was published…
        assert _sentinels() == []  # …and rollback released it
        assert ctx._components is None

    @pytest.mark.asyncio
    async def test_failed_storage_close_retains_sentinel(self, components) -> None:
        """A possibly-open store must stay advertised: ``storage_closed``
        is the release gate, and a close failure keeps the flock held."""
        components.storage = MagicMock()
        components.storage.close = AsyncMock(side_effect=RuntimeError("close failed"))
        ctx = await _init_flagged(components)
        (sentinel,) = _sentinels()
        await ctx.close()
        assert _sentinels() == [sentinel]
        reg_inst = ctx._instance_registration
        assert reg_inst is not None  # retained, not dropped
        # process-exit backstop still applies; release manually for hygiene
        reg_inst.cleanup()

    @pytest.mark.asyncio
    async def test_double_close_after_failed_storage_close_keeps_retaining(
        self, components
    ) -> None:
        """A second ``close()`` sees no components left; that absence must
        never be read as a confirmed storage close — the retained
        sentinel stays retained."""
        components.storage = MagicMock()
        components.storage.close = AsyncMock(side_effect=RuntimeError("close failed"))
        ctx = await _init_flagged(components)
        (sentinel,) = _sentinels()
        await ctx.close()
        await ctx.close()  # components are gone now — must not release
        assert _sentinels() == [sentinel]
        reg_inst = ctx._instance_registration
        assert reg_inst is not None
        reg_inst.cleanup()

    @pytest.mark.asyncio
    async def test_rollback_with_failed_close_then_lifespan_close_keeps_retaining(
        self, components, monkeypatch
    ) -> None:
        """Startup rollback with an unconfirmed storage close retains the
        sentinel; the lifespan's subsequent ``ctx.close()`` (no
        components) must not release it either."""
        from memtomem.indexing import watcher as watcher_mod

        components.storage = MagicMock()
        components.storage.close = AsyncMock(side_effect=RuntimeError("close failed"))

        def _broken_watcher(*_a: object, **_k: object) -> object:
            fake = MagicMock()
            fake.start = AsyncMock(side_effect=RuntimeError("watcher exploded"))
            fake.stop = AsyncMock()
            return fake

        monkeypatch.setattr(watcher_mod, "FileWatcher", _broken_watcher)
        ctx = AppContext(config=components.config, register_server_instance=True)
        with patch("memtomem.server.component_factory.create_components", return_value=components):
            with pytest.raises(RuntimeError, match="watcher exploded"):
                await ctx.ensure_initialized()
        (sentinel,) = _sentinels()  # retained by the rollback
        await ctx.close()  # lifespan shutdown after failed init
        assert _sentinels() == [sentinel]
        reg_inst = ctx._instance_registration
        assert reg_inst is not None
        reg_inst.cleanup()


class TestCancellation:
    @pytest.mark.asyncio
    async def test_cancel_during_registration_recovers_published_sentinel(
        self, components, monkeypatch
    ) -> None:
        """Cancelling the awaiting task cannot abandon the worker: the
        published handle is recovered and rollback releases it with
        close-before-cleanup ordering, then the cancellation propagates."""
        real_register = reg.register_instance

        def slow_register(path):
            time.sleep(0.3)
            return real_register(path)

        monkeypatch.setattr(reg, "register_instance", slow_register)
        ctx = AppContext(config=components.config, register_server_instance=True)
        with patch("memtomem.server.component_factory.create_components", return_value=components):
            task = asyncio.create_task(ctx.ensure_initialized())
            await asyncio.sleep(0.1)  # let the worker thread start
            task.cancel()
            await asyncio.sleep(0.05)
            task.cancel()  # repeated cancellation must not re-open the hole
            with pytest.raises(asyncio.CancelledError):
                await task
        assert _sentinels() == []
        assert ctx._components is None

    @pytest.mark.asyncio
    async def test_cancel_during_close_still_reaches_sentinel_release(self, components) -> None:
        """Accumulate-and-defer: a cancellation at the watcher-stop stage
        no longer skips component close + sentinel release; it re-raises
        after settlement."""
        ctx = await _init_flagged(components)
        assert len(_sentinels()) == 1
        cancelling_watcher = MagicMock()
        cancelling_watcher.stop = AsyncMock(side_effect=asyncio.CancelledError())
        ctx._watcher = cancelling_watcher
        with pytest.raises(asyncio.CancelledError):
            await ctx.close()
        assert _sentinels() == []
        assert ctx._components is None

    @pytest.mark.asyncio
    async def test_cancel_plus_storage_failure_retains_sentinel_and_reraises(
        self, components
    ) -> None:
        components.storage = MagicMock()
        components.storage.close = AsyncMock(side_effect=RuntimeError("close failed"))
        ctx = await _init_flagged(components)
        cancelling_watcher = MagicMock()
        cancelling_watcher.stop = AsyncMock(side_effect=asyncio.CancelledError())
        ctx._watcher = cancelling_watcher
        with pytest.raises(asyncio.CancelledError):
            await ctx.close()
        (sentinel,) = _sentinels()
        assert sentinel.exists()
        inst = ctx._instance_registration
        assert inst is not None
        inst.cleanup()
