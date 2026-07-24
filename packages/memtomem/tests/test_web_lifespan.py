"""Web lifespan auto-sync gating — follow-up to issue #349.

The web lifespan used to overwrite the runtime embedding config whenever the
DB-stored ``stored_embedding_info`` differed from config, then call
``storage.clear_embedding_mismatch()`` to suppress the mismatch banner. For
normal model drift (e.g. user edited ``config.json`` to a different onnx
model without running ``mm embedding-reset``) that soft-sync was benign.

For the dim=0 degraded-mode case introduced by #349, the stored "embedding"
is NoopEmbedder (``provider=none``, ``dim=0``) — auto-syncing silently
downgrades the user's configured onnx/bge-m3 to BM25-only AND swallows the
banner, so the user never sees the broken state and has no path to recover
it from the web UI. The gate below keeps the auto-sync only when the server
came up clean (``embedding_broken is None``) so the recovery banner +
``POST /api/embedding-reset`` flow stays reachable in degraded mode.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI

from memtomem.web.app import _lifespan


@dataclass
class _FakeEmbeddingCfg:
    provider: str = "onnx"
    model: str = "bge-m3"
    dimension: int = 1024


@dataclass
class _FakeIndexingCfg:
    memory_dirs: list = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.memory_dirs = self.memory_dirs or []


@dataclass
class _FakeSchedulerCfg:
    enabled: bool = False


@dataclass
class _FakePolicyCfg:
    enabled: bool = False


@dataclass
class _FakeConfig:
    embedding: _FakeEmbeddingCfg
    indexing: _FakeIndexingCfg
    scheduler: _FakeSchedulerCfg = field(default_factory=_FakeSchedulerCfg)
    policy: _FakePolicyCfg = field(default_factory=_FakePolicyCfg)


def _make_components(
    *,
    embedding_broken: dict[str, Any] | None,
    stored_info: dict[str, Any],
    cfg_provider: str = "onnx",
    cfg_model: str = "bge-m3",
    cfg_dim: int = 1024,
    scheduler_enabled: bool = False,
    policy_enabled: bool = False,
) -> MagicMock:
    """Build a mock ``Components`` for ``_lifespan``.

    ``storage.clear_embedding_mismatch`` and ``storage.stored_embedding_info``
    are probed by the auto-sync block; the rest is stubbed just enough to
    keep the context manager from raising before it yields.
    """
    storage = MagicMock()
    storage.stored_embedding_info = stored_info
    storage.clear_embedding_mismatch = MagicMock()

    comp = MagicMock()
    comp.config = _FakeConfig(
        embedding=_FakeEmbeddingCfg(provider=cfg_provider, model=cfg_model, dimension=cfg_dim),
        indexing=_FakeIndexingCfg(),
        scheduler=_FakeSchedulerCfg(enabled=scheduler_enabled),
        policy=_FakePolicyCfg(enabled=policy_enabled),
    )
    comp.storage = storage
    comp.embedder = MagicMock()
    comp.search_pipeline = MagicMock()
    comp.index_engine = MagicMock()
    comp.embedding_broken = embedding_broken
    return comp


async def _run_lifespan(comp: MagicMock) -> FastAPI:
    """Enter and exit ``_lifespan`` with a mocked ``create_components``.

    The FileWatcher patch keeps the lifespan from spinning a real
    watchdog Observer thread on every test. Tests that need to assert
    on ``watcher.start`` / ``watcher.stop`` directly patch FileWatcher
    themselves with a spy — see ``test_lifespan_starts_and_stops_file_watcher``.
    """
    app = FastAPI()
    fake_watcher = MagicMock()
    fake_watcher.start = AsyncMock()
    fake_watcher.stop = AsyncMock()
    with (
        patch("memtomem.server.component_factory.create_components", AsyncMock(return_value=comp)),
        patch("memtomem.server.component_factory.close_components", AsyncMock()),
        # The lifespan also instantiates DedupScanner — harmless to stub.
        patch("memtomem.search.dedup.DedupScanner", MagicMock()),
        patch("memtomem.indexing.watcher.FileWatcher", lambda *_a, **_kw: fake_watcher),
    ):
        async with _lifespan(app):
            pass
    return app


async def test_auto_sync_skipped_when_degraded():
    """With ``embedding_broken`` set, the lifespan must NOT overwrite config
    or clear the mismatch — the banner + reset button flow depends on those
    signals being left intact. Regression for issue #349 follow-up."""
    comp = _make_components(
        embedding_broken={
            "dimension_mismatch": True,
            "model_mismatch": True,
            "stored": {"dimension": 0, "provider": "none", "model": ""},
            "configured": {"dimension": 1024, "provider": "onnx", "model": "bge-m3"},
        },
        stored_info={"dimension": 0, "provider": "none", "model": ""},
    )

    await _run_lifespan(comp)

    # Config must still reflect what the user configured, not the legacy
    # NoopEmbedder meta row.
    assert comp.config.embedding.provider == "onnx"
    assert comp.config.embedding.model == "bge-m3"
    assert comp.config.embedding.dimension == 1024

    # The mismatch flag must survive so ``/api/embedding-status`` can surface
    # it and the UI banner can fire.
    comp.storage.clear_embedding_mismatch.assert_not_called()


async def test_auto_sync_runs_when_not_degraded():
    """Non-degraded model drift keeps the pre-#349 soft-sync behavior —
    config follows DB and the mismatch flag is cleared so the banner does
    not fire for drift that was already reconciled at startup."""
    comp = _make_components(
        embedding_broken=None,
        stored_info={"dimension": 384, "provider": "onnx", "model": "minilm-l12"},
        cfg_provider="onnx",
        cfg_model="bge-m3",
        cfg_dim=1024,
    )

    await _run_lifespan(comp)

    assert comp.config.embedding.model == "minilm-l12"
    assert comp.config.embedding.dimension == 384
    assert comp.config.embedding.provider == "onnx"
    comp.storage.clear_embedding_mismatch.assert_called_once()


@pytest.mark.parametrize(
    "stored_info",
    [
        None,
        {"dimension": 1024, "provider": "onnx", "model": "bge-m3"},  # matches config
    ],
)
async def test_auto_sync_noop_when_no_drift(stored_info):
    """When there's no stored info OR stored matches config, the sync block
    is a no-op regardless of ``embedding_broken`` — validates the gate
    doesn't accidentally enable sync on the non-drift path."""
    comp = _make_components(
        embedding_broken=None,
        stored_info=stored_info,
    )

    await _run_lifespan(comp)

    assert comp.config.embedding.provider == "onnx"
    comp.storage.clear_embedding_mismatch.assert_not_called()


async def test_scheduler_enabled_warns_in_web_lifespan(caplog):
    """``mm web`` does not run the schedule dispatcher (HealthWatchdog is
    wired only in the MCP server lifespan). Mirror the loud warning emitted
    by ``AppContext.ensure_initialized`` so users registering schedules
    against a web-only entry get a startup signal instead of silent
    null ``last_run_status``. Regression for issue #526."""
    import logging

    comp = _make_components(
        embedding_broken=None,
        stored_info=None,
        scheduler_enabled=True,
    )

    with caplog.at_level(logging.WARNING, logger="memtomem.web.app"):
        await _run_lifespan(comp)

    assert any(
        "scheduler.enabled=true" in r.message and "mm web" in r.message for r in caplog.records
    ), [r.message for r in caplog.records]


async def test_scheduler_disabled_no_warning(caplog):
    """No warning when scheduler is off — avoid noise on the default path."""
    import logging

    comp = _make_components(
        embedding_broken=None,
        stored_info=None,
        scheduler_enabled=False,
    )

    with caplog.at_level(logging.WARNING, logger="memtomem.web.app"):
        await _run_lifespan(comp)

    assert not any("scheduler.enabled" in r.message for r in caplog.records)


async def test_policy_enabled_warns_in_web_lifespan(caplog):
    """``mm web`` does not start ``PolicyScheduler`` (wired only in
    ``AppContext.ensure_initialized`` on the MCP server lifespan). Mirror the
    ``scheduler.enabled`` warning so users running ``mm web`` with
    ``policy.enabled=true`` see a loud signal at startup."""
    import logging

    comp = _make_components(
        embedding_broken=None,
        stored_info=None,
        policy_enabled=True,
    )

    with caplog.at_level(logging.WARNING, logger="memtomem.web.app"):
        await _run_lifespan(comp)

    assert any(
        "policy.enabled=true" in r.message and "mm web" in r.message for r in caplog.records
    ), [r.message for r in caplog.records]


async def test_policy_disabled_no_warning(caplog):
    """No warning when policy is off — avoid noise on the default path."""
    import logging

    comp = _make_components(
        embedding_broken=None,
        stored_info=None,
        policy_enabled=False,
    )

    with caplog.at_level(logging.WARNING, logger="memtomem.web.app"):
        await _run_lifespan(comp)

    assert not any("policy.enabled" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# FileWatcher wiring — guards the regression where ``mm web`` ran with no
# fs watcher at all. Files added to memory_dirs (whether while the server
# was up, or before the dir was registered) were never auto-picked-up
# until the user clicked Reindex. The lifespan now wires the same
# FileWatcher that ``server/context.py`` uses, gated on the same
# degraded-mode check.
# ---------------------------------------------------------------------------


async def test_lifespan_starts_and_stops_file_watcher():
    """Watcher started on lifespan entry, stopped on exit, exposed on
    ``app.state.file_watcher`` so routes / shutdown handlers can find it.
    """
    fake_watcher = MagicMock()
    fake_watcher.start = AsyncMock()
    fake_watcher.stop = AsyncMock()
    comp = _make_components(embedding_broken=None, stored_info=None)

    app = FastAPI()
    with (
        patch("memtomem.server.component_factory.create_components", AsyncMock(return_value=comp)),
        patch("memtomem.server.component_factory.close_components", AsyncMock()),
        patch("memtomem.search.dedup.DedupScanner", MagicMock()),
        patch("memtomem.indexing.watcher.FileWatcher", lambda *_a, **_kw: fake_watcher),
    ):
        async with _lifespan(app):
            assert fake_watcher.start.await_count == 1
            assert app.state.file_watcher is fake_watcher
            assert app.state.startup_state == "ready"
            assert app.state.config_signature is not None
            assert app.state.last_reload_error is None

    assert fake_watcher.stop.await_count == 1
    assert app.state.startup_state == "not_started"


async def test_lifespan_skips_watcher_in_degraded_mode():
    """When embedding is broken, the watcher must NOT start — the
    indexer would crash on the missing ``chunks_vec`` table. Recovery
    happens via ``mem_embedding_reset``; mirrors the same guard in
    ``server/context.py``.
    """
    fake_watcher = MagicMock()
    fake_watcher.start = AsyncMock()
    fake_watcher.stop = AsyncMock()
    comp = _make_components(
        embedding_broken={
            "dimension_mismatch": True,
            "model_mismatch": True,
            "stored": {"dimension": 0, "provider": "none", "model": ""},
            "configured": {"dimension": 1024, "provider": "onnx", "model": "bge-m3"},
        },
        stored_info={"dimension": 0, "provider": "none", "model": ""},
    )

    app = FastAPI()
    with (
        patch("memtomem.server.component_factory.create_components", AsyncMock(return_value=comp)),
        patch("memtomem.server.component_factory.close_components", AsyncMock()),
        patch("memtomem.search.dedup.DedupScanner", MagicMock()),
        patch("memtomem.indexing.watcher.FileWatcher", lambda *_a, **_kw: fake_watcher),
    ):
        async with _lifespan(app):
            pass

    assert fake_watcher.start.await_count == 0
    assert fake_watcher.stop.await_count == 0
    assert not hasattr(app.state, "file_watcher")


# ---------------------------------------------------------------------------
# Lifecycle barrier participation (#1952). ``mm web`` must take the shared
# lifecycle barrier (#1936) BEFORE storage opens, the same ordering
# ``AppContext.ensure_initialized`` uses for the MCP server, so the exclusive
# hold of ``mm uninstall`` (#1944) / ``mm reset`` (#1945) excludes a
# concurrently-starting ``mm web`` instead of racing its under-barrier
# liveness re-probe. Release polarity mirrors the server: a confirmed storage
# close drops the hold, an unconfirmed close retains it until process exit.
#
# The autouse ``_isolated_instance_registry`` fixture (conftest) redirects the
# barrier path to a per-test dir and sweeps leaks at teardown — but that sweep
# is a safety net, NOT a verification tool, so every release/retain assertion
# below inspects the captured ``HeldBarrier`` inside the test.
# ---------------------------------------------------------------------------


def _capturing_acquire(captured: list) -> Any:
    """Wrap the real shared acquire so tests can inspect the returned handle.

    Binds the genuine acquire at construction time (before the module attr is
    patched to this wrapper) so it takes the real flock — an isolated one, per
    the autouse registry fixture — and appends the handle to ``captured``.
    """
    import memtomem._instance_registry as reg

    real_acquire = reg.acquire_server_lifecycle_barrier

    def _acquire(timeout_s=None):
        barrier = real_acquire(timeout_s)
        captured.append(barrier)
        return barrier

    return _acquire


async def _run_lifespan_with_barrier(
    comp: MagicMock | None,
    *,
    close_result: Any = None,
    create_side_effect: BaseException | None = None,
    watcher_start_error: BaseException | None = None,
    expect_exc: type[BaseException] | None = None,
) -> tuple[FastAPI, list]:
    """Enter/exit ``_lifespan`` with the real barrier acquire captured.

    The lifespan helper imports ``acquire_server_lifecycle_barrier`` lazily, so
    patching the module attribute with a capturing wrapper intercepts it while
    still taking the real (isolated) flock. ``expect_exc`` wraps the context
    manager in ``pytest.raises`` for the startup-failure paths that re-raise.
    Returns ``(app, captured_barriers)`` — callers assert on the handle state.
    """
    import memtomem._instance_registry as reg

    captured: list = []
    acquire = _capturing_acquire(captured)

    create_mock = AsyncMock(return_value=comp)
    if create_side_effect is not None:
        create_mock.side_effect = create_side_effect
    close_mock = AsyncMock(return_value=close_result)

    fake_watcher = MagicMock()
    fake_watcher.start = AsyncMock(side_effect=watcher_start_error)
    fake_watcher.stop = AsyncMock()

    app = FastAPI()
    with (
        patch.object(reg, "acquire_server_lifecycle_barrier", acquire),
        patch("memtomem.server.component_factory.create_components", create_mock),
        patch("memtomem.server.component_factory.close_components", close_mock),
        patch("memtomem.search.dedup.DedupScanner", MagicMock()),
        patch("memtomem.indexing.watcher.FileWatcher", lambda *_a, **_kw: fake_watcher),
    ):
        if expect_exc is not None:
            with pytest.raises(expect_exc):
                async with _lifespan(app):
                    pass
        else:
            async with _lifespan(app):
                pass
    return app, captured


def _teardown(*, storage_closed: bool):
    from memtomem.server.component_factory import TeardownResult

    return TeardownResult(storage_closed=storage_closed)


async def test_lifespan_takes_barrier_before_create_components():
    """The barrier must be acquired before storage opens — the whole point of
    #1952. An acquire that lands after ``create_components`` would leave the
    same TOCTOU window the barrier closes."""
    import memtomem._instance_registry as reg

    order: list[str] = []
    real_acquire = reg.acquire_server_lifecycle_barrier

    def _acquire(timeout_s=None):
        order.append("acquire")
        return real_acquire(timeout_s)

    comp = _make_components(embedding_broken=None, stored_info=None)
    create_mock = AsyncMock(return_value=comp)
    create_mock.side_effect = lambda: order.append("create") or comp

    fake_watcher = MagicMock()
    fake_watcher.start = AsyncMock()
    fake_watcher.stop = AsyncMock()

    app = FastAPI()
    with (
        patch.object(reg, "acquire_server_lifecycle_barrier", _acquire),
        patch("memtomem.server.component_factory.create_components", create_mock),
        patch("memtomem.server.component_factory.close_components", AsyncMock()),
        patch("memtomem.search.dedup.DedupScanner", MagicMock()),
        patch("memtomem.indexing.watcher.FileWatcher", lambda *_a, **_kw: fake_watcher),
    ):
        async with _lifespan(app):
            pass

    assert order[:2] == ["acquire", "create"]


async def test_lifespan_refused_barrier_never_opens_storage():
    """A ``BarrierTimeout`` (uninstall/reset holds the barrier exclusive) must
    fail startup closed — ``create_components`` is never reached and the state
    is ``failed``. Starting unbarriered would defeat the exclusion."""
    import memtomem._instance_registry as reg

    def _acquire(timeout_s=None):
        raise reg.BarrierTimeout("busy")

    create_mock = AsyncMock()
    app = FastAPI()
    with (
        patch.object(reg, "acquire_server_lifecycle_barrier", _acquire),
        patch("memtomem.server.component_factory.create_components", create_mock),
        patch("memtomem.server.component_factory.close_components", AsyncMock()),
    ):
        with pytest.raises(reg.BarrierTimeout):
            async with _lifespan(app):
                pass

    create_mock.assert_not_awaited()
    assert app.state.startup_state == "failed"


async def test_lifespan_barrier_oserror_never_opens_storage():
    """An unusable barrier path (``OSError``) is infrastructure, not
    contention — it must also fail startup closed without opening storage.
    Pinned separately from the ``BarrierTimeout`` path: each cause its own
    ``except`` arm."""
    import memtomem._instance_registry as reg

    def _acquire(timeout_s=None):
        raise PermissionError("barrier path unwritable")

    create_mock = AsyncMock()
    app = FastAPI()
    with (
        patch.object(reg, "acquire_server_lifecycle_barrier", _acquire),
        patch("memtomem.server.component_factory.create_components", create_mock),
        patch("memtomem.server.component_factory.close_components", AsyncMock()),
    ):
        with pytest.raises(PermissionError):
            async with _lifespan(app):
                pass

    create_mock.assert_not_awaited()
    assert app.state.startup_state == "failed"


async def test_lifespan_refusal_logs_actionable_message(caplog):
    """The contention refusal must name the destructive command and the
    remediation so the uvicorn/CLI failure path surfaces something the user
    can act on."""
    import logging

    import memtomem._instance_registry as reg

    def _acquire(timeout_s=None):
        raise reg.BarrierTimeout("busy after 2.0s")

    app = FastAPI()
    with (
        patch.object(reg, "acquire_server_lifecycle_barrier", _acquire),
        patch("memtomem.server.component_factory.create_components", AsyncMock()),
        patch("memtomem.server.component_factory.close_components", AsyncMock()),
        caplog.at_level(logging.ERROR, logger="memtomem.web.app"),
    ):
        with pytest.raises(reg.BarrierTimeout):
            async with _lifespan(app):
                pass

    msgs = [r.message for r in caplog.records]
    assert any("mm uninstall" in m and "re-run mm web" in m for m in msgs), msgs


async def test_lifespan_releases_barrier_on_confirmed_close():
    """A confirmed storage close drops the hold so a later uninstall/reset is
    not blocked by a web process that already exited cleanly."""
    comp = _make_components(embedding_broken=None, stored_info=None)
    _, captured = await _run_lifespan_with_barrier(
        comp, close_result=_teardown(storage_closed=True)
    )

    assert len(captured) == 1
    assert captured[0]._closed is True


async def test_lifespan_retains_barrier_on_unconfirmed_close(caplog):
    """An unconfirmed storage close RETAINS the hold (#1936 polarity): a
    possibly-open store must keep blocking uninstall until process exit."""
    import logging

    comp = _make_components(embedding_broken=None, stored_info=None)
    with caplog.at_level(logging.WARNING, logger="memtomem.web.app"):
        _, captured = await _run_lifespan_with_barrier(
            comp, close_result=_teardown(storage_closed=False)
        )

    assert len(captured) == 1
    assert captured[0]._closed is False
    assert any("retaining lifecycle barrier" in r.message for r in caplog.records)
    captured[0].release()  # hygiene — do not leak past the test


async def test_lifespan_retains_barrier_when_create_components_fails():
    """When ``create_components`` raises, ``comp`` is None so there is no
    ``TeardownResult`` to confirm a close. The factory rollback closes storage
    best-effort and discards close failures, so the store may still be open in
    this doomed process — the barrier must be RETAINED (fail closed), not
    released, until process exit frees the flock (#1936 polarity; Codex #1952
    blocker)."""
    _, captured = await _run_lifespan_with_barrier(
        None, create_side_effect=RuntimeError("boom"), expect_exc=RuntimeError
    )

    assert len(captured) == 1
    assert captured[0]._closed is False
    captured[0].release()  # hygiene — do not leak past the test


async def test_lifespan_releases_barrier_when_pre_yield_step_fails():
    """A failure between storage open and yield (here: watcher start) still
    releases the barrier when the storage close is confirmed."""
    comp = _make_components(embedding_broken=None, stored_info=None)
    _, captured = await _run_lifespan_with_barrier(
        comp,
        close_result=_teardown(storage_closed=True),
        watcher_start_error=RuntimeError("watcher boom"),
        expect_exc=RuntimeError,
    )

    assert len(captured) == 1
    assert captured[0]._closed is True


# --- cross-process contention infra (spawn) ---------------------------------
# flock / LockFileEx are process-level and Windows may grant a second
# same-process handle, so an in-process contender proves nothing — the barrier
# suite validates contention cross-process (see ``test_lifecycle_barrier.py``).
# The two wiring tests below reuse that spawn pattern: a child holds one lock
# mode while the web lifespan takes the other in this process.

_CTX = mp.get_context("spawn")


def _child_setup(rt_str: str):
    import memtomem._instance_registry as _reg

    target = Path(rt_str)

    def _rt() -> Path:
        return target

    def _ensure() -> Path:
        target.mkdir(mode=0o700, exist_ok=True)
        return target

    _reg.runtime_dir = _rt
    _reg.ensure_runtime_dir = _ensure
    return _reg


def _child_hold_exclusive(rt_str: str, q, release) -> None:
    _reg = _child_setup(rt_str)
    barrier = _reg.acquire_uninstall_lifecycle_barrier()
    q.put(("held", os.getpid()))
    release.wait(60)
    barrier.release()


def _child_try_exclusive(rt_str: str, q, timeout_s: float) -> None:
    _reg = _child_setup(rt_str)
    try:
        barrier = _reg.acquire_uninstall_lifecycle_barrier(timeout_s=timeout_s)
    except _reg.BarrierTimeout:
        q.put(("result", "refused"))
    else:
        q.put(("result", "acquired"))
        barrier.release()


def _drain_until(q, tag: str, timeout: float = 30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            msg = q.get(timeout=1.0)
        except Exception:
            continue
        if msg[0] == tag:
            return msg
    raise AssertionError(f"child never reported {tag!r}")


def _stop(proc) -> None:
    if proc.is_alive():
        proc.kill()
    proc.join(timeout=30)


@pytest.fixture
def rt(tmp_path, monkeypatch) -> Path:
    """A barrier-of-record dir shared with spawned children by path string.

    Overrides the conftest autouse isolation (which monkeypatches the same
    resolvers at a dir the spawned, fixture-blind children can't see) so a
    cross-process contender and this process lock the same file.
    """
    import memtomem._instance_registry as reg

    target = tmp_path / "rt"

    def _ensure() -> Path:
        target.mkdir(mode=0o700, exist_ok=True)
        return target

    monkeypatch.setattr(reg, "runtime_dir", lambda: target)
    monkeypatch.setattr(reg, "ensure_runtime_dir", _ensure)
    return target


async def test_exclusive_holder_blocks_web_lifespan(rt, monkeypatch):
    """Headline regression for #1952: while ``mm uninstall`` / ``mm reset``
    holds the barrier EXCLUSIVE (from a separate process), a ``mm web`` startup
    is refused and never opens storage.

    Cross-process by construction (the exclusive holder is a spawned child) —
    an in-process contender proves nothing on Windows, where the same-process
    handle may be granted. The parent's acquire budget is shortened so the
    refusal resolves fast."""
    import memtomem._instance_registry as reg

    monkeypatch.setattr(reg, "_BARRIER_TIMEOUT_S", 0.3)
    q, release = _CTX.Queue(), _CTX.Event()
    holder = _CTX.Process(target=_child_hold_exclusive, args=(str(rt), q, release))
    holder.start()
    try:
        _drain_until(q, "held")
        create_mock = AsyncMock()
        app = FastAPI()
        with (
            patch("memtomem.server.component_factory.create_components", create_mock),
            patch("memtomem.server.component_factory.close_components", AsyncMock()),
        ):
            with pytest.raises(reg.BarrierTimeout):
                async with _lifespan(app):
                    pass
        create_mock.assert_not_awaited()
    finally:
        release.set()
        holder.join(timeout=30)
        _stop(holder)


async def test_web_lifespan_blocks_exclusive_acquire(rt):
    """The converse: while ``mm web``'s lifespan holds the barrier SHARED, an
    exclusive acquire (uninstall/reset) from a separate process is refused —
    proving the shared hold is really taken for the storage lifetime. The
    exclusive contender runs in a spawned child (cross-process, per the repo
    convention); the lifespan takes the real (isolated) shared flock."""
    comp = _make_components(embedding_broken=None, stored_info=None)
    fake_watcher = MagicMock()
    fake_watcher.start = AsyncMock()
    fake_watcher.stop = AsyncMock()

    app = FastAPI()
    with (
        patch("memtomem.server.component_factory.create_components", AsyncMock(return_value=comp)),
        patch("memtomem.server.component_factory.close_components", AsyncMock()),
        patch("memtomem.search.dedup.DedupScanner", MagicMock()),
        patch("memtomem.indexing.watcher.FileWatcher", lambda *_a, **_kw: fake_watcher),
    ):
        async with _lifespan(app):
            q = _CTX.Queue()
            child = _CTX.Process(target=_child_try_exclusive, args=(str(rt), q, 0.3))
            child.start()
            try:
                outcome = _drain_until(q, "result")
                assert outcome[1] == "refused", outcome
            finally:
                child.join(timeout=30)
                _stop(child)
