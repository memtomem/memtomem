"""AppContext ↔ instance-registry wiring (#1935) and lifecycle barrier (#1936).

Covers the server-only opt-in flag, publication at storage init,
close-before-cleanup ordering (sentinel released only on a *confirmed*
storage close), rollback release, and cancellation accumulate-and-defer.
Registry lock semantics themselves are covered cross-process in
``test_instance_registry.py``; here the registry runs for real but inside
the per-test isolated runtime dir (conftest ``_isolated_instance_registry``).

``TestLifecycleBarrierOwnership`` extends the same discipline to the
#1936 barrier, which shares the ``storage_closed`` release gate. Its
release assertions never lean on fixture teardown (which sweeps leaked
barriers and would mask a bug): each one proves the barrier is free by
having a **spawned process** take it exclusively before the test ends.
Same-process re-acquisition would be weaker — Windows can grant a second
handle to the owning process, so a dropped ``release()`` could pass there.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
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


def _child_try_exclusive(rt_str: str, q) -> None:
    """Attempt the conflicting acquire from a separate process."""
    import memtomem._instance_registry as _reg

    target = Path(rt_str)
    _reg.runtime_dir = lambda: target
    _reg.ensure_runtime_dir = lambda: (target.mkdir(mode=0o700, exist_ok=True), target)[1]
    try:
        _reg.acquire_uninstall_lifecycle_barrier(timeout_s=1.0).release()
    except Exception as exc:  # noqa: BLE001 — the message is the signal
        q.put(("refused", type(exc).__name__))
        return
    q.put(("acquired", ""))


def _assert_barrier_free(rt: Path) -> None:
    """Fail unless a *spawned* process can take the barrier exclusively."""
    import multiprocessing as mp

    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    child = ctx.Process(target=_child_try_exclusive, args=(str(rt), q))
    child.start()
    try:
        outcome, detail = q.get(timeout=30)
    finally:
        child.join(timeout=30)
        if child.is_alive():
            child.kill()
            child.join(timeout=30)
    assert outcome == "acquired", f"barrier still held ({detail})"


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


_OPT_IN = "register_server_instance"
_REMEDY = (
    "Registering as an MCP server instance (#1935) is a lifespan-only "
    "privilege: only a process that IS the MCP server may advertise the "
    "store it holds open. If you genuinely need a second opt-in, say so "
    "in the PR and update this guard deliberately — do not widen it to "
    "make a red test pass."
)


def _package_trees(src: Path):
    for path in sorted(src.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        yield path.relative_to(src).as_posix(), tree


class TestOptIn:
    """The registry opt-in must have exactly one call site, and the guard
    proving it must catch every *conventional* spelling of a second one.

    An earlier version grepped the package text for
    ``register_server_instance=True``; a variable value, a positional
    argument, or a post-hoc assignment all evaded it. These checks parse
    instead, and deliberately ignore the callee name — a keyword reaches
    the flag through an alias, a factory wrapper, or
    ``dataclasses.replace`` just as well as through ``AppContext(...)``.

    Scope, stated honestly: this is a mistake-guard, not an adversary-
    guard. It catches the flag's name as a keyword (any call), as an
    attribute-assignment target, and as a string literal (``setattr`` /
    ``__dict__`` / dict-key spellings). A name computed at runtime evades
    it — that is review territory, not test territory.
    """

    def test_exactly_one_opt_in_keyword_package_wide(self) -> None:
        # Deliberately callee-agnostic and value-agnostic when *counting*:
        # every occurrence of the keyword is a hit, including
        # ``register_server_instance=False`` on some unrelated call. That
        # over-counts by design — a keyword with this name is a thing a
        # human should look at, wherever it appears — so the failure
        # message says "occurrence", not "opt-in", to keep a false
        # positive from reading as an accusation.
        src = Path(memtomem.__file__).parent
        occurrences = [
            (rel, kw.value)
            for rel, tree in _package_trees(src)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            for kw in node.keywords
            if kw.arg == _OPT_IN
        ]
        assert len(occurrences) == 1, (
            f"expected one `{_OPT_IN}=` keyword in the package, found "
            f"{[r for r, _ in occurrences]}. {_REMEDY}"
        )
        rel, value = occurrences[0]
        assert rel == "server/lifespan.py", f"the `{_OPT_IN}=` keyword moved to {rel}. {_REMEDY}"
        # Pin the value, not just the keyword: ``=False`` would silently
        # disable the whole feature while satisfying a name-only search.
        assert isinstance(value, ast.Constant) and value.value is True, (
            "the opt-in must pass a literal True — a variable makes the flag "
            "runtime-dependent and unauditable"
        )

    def test_flag_is_never_assigned_after_construction(self) -> None:
        src = Path(memtomem.__file__).parent
        writes = [
            (rel, node.lineno)
            for rel, tree in _package_trees(src)
            for node in ast.walk(tree)
            if (
                isinstance(node, ast.Attribute)
                and node.attr == _OPT_IN
                and isinstance(node.ctx, ast.Store)
            )
        ]
        assert writes == [], f"post-construction opt-in at {writes}. {_REMEDY}"

    def test_flag_name_never_appears_as_a_string_literal(self) -> None:
        """``setattr(ctx, "register_server_instance", True)``, a
        ``__dict__`` write, and ``AppContext(**{"register_server_…": x})``
        all smuggle the flag without a keyword or an attribute node —
        but each one has to spell the name as a string constant."""
        src = Path(memtomem.__file__).parent
        mentions = [
            (rel, node.lineno)
            for rel, tree in _package_trees(src)
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and node.value == _OPT_IN
        ]
        assert mentions == [], f"flag name as a string literal at {mentions}. {_REMEDY}"

    def test_opt_in_is_keyword_only_and_released_positional_order_is_intact(self) -> None:
        """Keyword-only is what makes the keyword scan complete — a
        positional opt-in would never spell the name. Equally load-bearing
        in the other direction: the flag must NOT occupy a positional
        slot, because ``AppContext``'s positional order (``config,
        webhook_manager, current_session_id, …``) shipped in v0.3.x and
        splicing the flag into slot 3 would re-bind released call shapes.
        """
        params = inspect.signature(AppContext.__init__).parameters
        assert params[_OPT_IN].kind is inspect.Parameter.KEYWORD_ONLY
        positional = [
            n
            for n, p in params.items()
            if p.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD and n != "self"
        ]
        assert positional[:4] == [
            "config",
            "webhook_manager",
            "current_session_id",
            "current_agent_id",
        ]

    def test_released_positional_call_shape_still_binds_session_id(self) -> None:
        ctx = AppContext(Mem2MemConfig(), None, "sess-id")
        assert ctx.current_session_id == "sess-id"
        assert ctx.register_server_instance is False

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


class TestLifecycleBarrierOwnership:
    """Who holds the #1936 barrier, and when it is safe to let go.

    Every release assertion re-acquires the barrier *inside the test*: the
    autouse fixture sweeps leaked holds at teardown, so a passing suite is
    no evidence that production code released anything.
    """

    @staticmethod
    def _reacquire_or_fail() -> None:
        """Prove the barrier is free right now, from another *process*.

        Same-process re-acquisition would be a weaker check than it looks:
        Windows can grant a second handle in the owning process (the
        reason ``test_lifecycle_barrier.py`` is spawn-based throughout),
        so a dropped ``release()`` could pass there and then be hidden by
        the autouse sweep at teardown.
        """
        _assert_barrier_free(reg.lifecycle_barrier_path().parent)

    @pytest.mark.asyncio
    async def test_barrier_is_held_before_storage_opens(self, components) -> None:
        """The ordering that closes the race: by the time
        ``create_components`` runs, the barrier is already ours."""
        observed: list[object] = []

        def _create(*_a: object, **_k: object):
            observed.append(ctx._lifecycle_barrier)
            return components

        ctx = AppContext(config=components.config, register_server_instance=True)
        with patch("memtomem.server.component_factory.create_components", side_effect=_create):
            await ctx.ensure_initialized()
        try:
            assert observed, "create_components was never reached"
            assert observed[0] is not None, "storage opened before the barrier was held"
        finally:
            await ctx.close()

    @pytest.mark.asyncio
    async def test_unflagged_context_never_takes_the_barrier(self, components) -> None:
        """CLI / ``mm web`` / LangGraph build components through the same
        factory and must stay out of the barrier protocol entirely."""
        ctx = AppContext(config=components.config)
        with patch("memtomem.server.component_factory.create_components", return_value=components):
            await ctx.ensure_initialized()
        try:
            assert ctx._lifecycle_barrier is None
            self._reacquire_or_fail()
        finally:
            await ctx.close()

    @pytest.mark.asyncio
    async def test_barrier_outlives_registration_and_is_released_on_close(self, components) -> None:
        ctx = await _init_flagged(components)
        assert ctx._lifecycle_barrier is not None
        await ctx.close()
        assert ctx._lifecycle_barrier is None
        self._reacquire_or_fail()

    @pytest.mark.asyncio
    async def test_registration_failure_still_leaves_the_barrier_held(
        self, components, monkeypatch
    ) -> None:
        """The hole lifetime-hold exists to close: registration returns
        ``None`` (its documented failure mode), so nothing advertises the
        open store — only the barrier keeps uninstall out."""
        monkeypatch.setattr(reg, "register_instance", lambda _p: None)
        ctx = await _init_flagged(components)
        try:
            assert _sentinels() == []  # nothing published…
            assert ctx._lifecycle_barrier is not None  # …but still blocking
            with pytest.raises(reg.BarrierTimeout):
                reg.acquire_uninstall_lifecycle_barrier(timeout_s=0.3)
        finally:
            await ctx.close()

    @pytest.mark.asyncio
    async def test_failed_storage_close_retains_the_barrier(self, components) -> None:
        """Same polarity as the sentinel: a possibly-open store keeps
        blocking uninstall until the process exits."""
        components.storage = MagicMock()
        components.storage.close = AsyncMock(side_effect=RuntimeError("close failed"))
        ctx = await _init_flagged(components)
        await ctx.close()
        barrier = ctx._lifecycle_barrier
        assert barrier is not None, "unconfirmed close must retain the barrier"
        with pytest.raises(reg.BarrierTimeout):
            reg.acquire_uninstall_lifecycle_barrier(timeout_s=0.3)
        # process-exit backstop still applies; release manually for hygiene
        barrier.release()
        inst = ctx._instance_registration
        if inst is not None:
            inst.cleanup()

    @pytest.mark.asyncio
    async def test_double_close_after_failed_storage_close_keeps_retaining(
        self, components
    ) -> None:
        """A second close sees no components; that absence must not be
        read as a confirmed storage close."""
        components.storage = MagicMock()
        components.storage.close = AsyncMock(side_effect=RuntimeError("close failed"))
        ctx = await _init_flagged(components)
        await ctx.close()
        await ctx.close()
        barrier = ctx._lifecycle_barrier
        assert barrier is not None
        with pytest.raises(reg.BarrierTimeout):
            reg.acquire_uninstall_lifecycle_barrier(timeout_s=0.3)
        barrier.release()
        inst = ctx._instance_registration
        if inst is not None:
            inst.cleanup()

    @pytest.mark.asyncio
    async def test_rollback_after_post_storage_failure_releases_the_barrier(
        self, components, monkeypatch
    ) -> None:
        """A clean storage close during startup rollback frees it."""
        from memtomem.indexing import watcher as watcher_mod

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
        assert ctx._lifecycle_barrier is None
        self._reacquire_or_fail()

    @pytest.mark.asyncio
    async def test_acquire_failure_propagates_and_storage_never_opens(
        self, components, monkeypatch
    ) -> None:
        """Fail closed: the whole point is that a refused barrier stops
        initialization *before* the store is opened."""
        opened: list[int] = []

        def _create(*_a: object, **_k: object):
            opened.append(1)
            return components

        def _refuse(*_a: object, **_k: object):
            raise reg.BarrierTimeout("busy")

        monkeypatch.setattr(reg, "acquire_server_lifecycle_barrier", _refuse)
        ctx = AppContext(config=components.config, register_server_instance=True)
        with patch("memtomem.server.component_factory.create_components", side_effect=_create):
            with pytest.raises(reg.BarrierTimeout):
                await ctx.ensure_initialized()
        assert opened == [], "storage must not open when the barrier is refused"
        assert ctx._components is None
        assert ctx._lifecycle_barrier is None

    @pytest.mark.asyncio
    async def test_retry_after_a_refused_acquire_succeeds(self, components, monkeypatch) -> None:
        """Init is lazy and retried per tool call: once the uninstall has
        finished, the next attempt must go through."""
        calls: list[int] = []
        real_acquire = reg.acquire_server_lifecycle_barrier

        def _flaky(*a: object, **k: object):
            calls.append(1)
            if len(calls) == 1:
                raise reg.BarrierTimeout("busy")
            return real_acquire(*a, **k)

        monkeypatch.setattr(reg, "acquire_server_lifecycle_barrier", _flaky)
        ctx = AppContext(config=components.config, register_server_instance=True)
        with patch("memtomem.server.component_factory.create_components", return_value=components):
            with pytest.raises(reg.BarrierTimeout):
                await ctx.ensure_initialized()
            await ctx.ensure_initialized()
        try:
            assert len(calls) == 2
            assert ctx._lifecycle_barrier is not None
        finally:
            await ctx.close()

    @pytest.mark.asyncio
    async def test_cancel_during_acquire_releases_the_handle(self, components, monkeypatch) -> None:
        """A cancelled acquire must not leave a barrier behind.

        ``settle_shielded_value`` hands the handle back so it cannot be
        dropped on the floor, but storage never opened here — the
        cancellation propagates *before* the block whose rollback would
        release it. Retaining would block ``mm uninstall`` on behalf of a
        server that never opened the store, with no release path short of
        process exit. Verified without touching the handle by hand: the
        earlier version of this test released it manually and would have
        passed against exactly that bug.
        """
        real_acquire = reg.acquire_server_lifecycle_barrier

        def _slow(*a: object, **k: object):
            time.sleep(0.3)
            return real_acquire(*a, **k)

        monkeypatch.setattr(reg, "acquire_server_lifecycle_barrier", _slow)
        ctx = AppContext(config=components.config, register_server_instance=True)
        with patch("memtomem.server.component_factory.create_components", return_value=components):
            task = asyncio.create_task(ctx.ensure_initialized())
            await asyncio.sleep(0.1)  # let the worker thread start
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert ctx._components is None
        assert ctx._lifecycle_barrier is None
        self._reacquire_or_fail()

    @pytest.mark.asyncio
    async def test_close_after_cancelled_acquire_leaves_nothing_held(
        self, components, monkeypatch
    ) -> None:
        """The lifespan's shutdown path after a cancelled init: ``close()``
        sees no components and so cannot confirm a storage close — which
        must not translate into a retained barrier, because there was
        never an open store to protect."""
        real_acquire = reg.acquire_server_lifecycle_barrier

        def _slow(*a: object, **k: object):
            time.sleep(0.3)
            return real_acquire(*a, **k)

        monkeypatch.setattr(reg, "acquire_server_lifecycle_barrier", _slow)
        ctx = AppContext(config=components.config, register_server_instance=True)
        with patch("memtomem.server.component_factory.create_components", return_value=components):
            task = asyncio.create_task(ctx.ensure_initialized())
            await asyncio.sleep(0.1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        await ctx.close()
        self._reacquire_or_fail()
