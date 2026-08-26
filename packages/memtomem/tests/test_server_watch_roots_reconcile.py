"""Issue #2186: the MCP server reconciles its watched roots with the config file.

`AppContext` builds its `FileWatcher` once, at init. Nothing afterwards told it
that a memory dir was added or removed — and nothing can, because no MCP tool is
able to mutate `indexing.memory_dirs` (it is absent from `MUTABLE_FIELDS` and
`_set_config_key` refuses list fields outright). Every real writer is another
process: `mm init`, `mm mem init`, `mm config unset`, the web UI, or an editor.
They rewrite `~/.memtomem/config.json` and nothing else, so the server has to
look at the file. It does, once per tool call, off a stat-level signature.

These tests pin that reconciliation, its failure modes, and the fact that the
per-call cost stays a stat when nothing changed.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from memtomem.config import Mem2MemConfig
from memtomem.server.component_factory import Components
from memtomem.server.context import AppContext


@pytest.fixture(autouse=True)
def _isolated_config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the config path helpers at a tmp HOME and strip MEMTOMEM_* env.

    The signature and the strict rebuild both read the real
    ``~/.memtomem/config.json`` otherwise, which would make these tests depend
    on the developer's own install (and, worse, let a test write to it).
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    for key in list(__import__("os").environ):
        if key.startswith("MEMTOMEM_"):
            monkeypatch.delenv(key, raising=False)
    (tmp_path / ".memtomem").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _write_config(home: Path, memory_dirs: list[Path]) -> None:
    path = home / ".memtomem" / "config.json"
    path.write_text(
        json.dumps({"indexing": {"memory_dirs": [str(d) for d in memory_dirs]}}),
        encoding="utf-8",
    )


def _roots(ctx: AppContext) -> set[Path]:
    """Resolved index roots.

    The saved config collapses the home prefix back to ``~``, so comparing
    against the literal tmp paths would fail on a value that is in fact
    correct — expand before comparing rather than pinning the spelling.
    """
    return {Path(p).expanduser().resolve() for p in ctx.config.indexing.all_index_roots()}


def _fake_watcher() -> MagicMock:
    watcher = MagicMock(name="watcher")
    watcher.start = AsyncMock()
    watcher.stop = AsyncMock()
    watcher.reconfigure = AsyncMock()
    return watcher


async def _make_app(home: Path, memory_dirs: list[Path]) -> tuple[AppContext, MagicMock]:
    """A lifespan-owned context whose watcher is a stub, seeded from disk."""
    from memtomem.indexing import watcher as watcher_mod

    _write_config(home, memory_dirs)
    config = Mem2MemConfig()
    config.indexing.memory_dirs = list(memory_dirs)
    components = Components(
        config=config,
        storage=object(),  # type: ignore[arg-type]
        embedder=object(),  # type: ignore[arg-type]
        index_engine=object(),  # type: ignore[arg-type]
        search_pipeline=object(),  # type: ignore[arg-type]
    )
    watcher = _fake_watcher()
    ctx = AppContext(config=config)
    with (
        patch(
            "memtomem.server.component_factory.create_components",
            return_value=components,
        ),
        patch.object(watcher_mod, "FileWatcher", lambda *a, **k: watcher),
    ):
        await ctx.ensure_initialized()
    return ctx, watcher


@pytest.mark.asyncio
async def test_added_root_is_watched_without_a_restart(
    _isolated_config_home: Path, tmp_path: Path
) -> None:
    """The acceptance criterion: another process adds a memory dir, and the
    next tool call watches it — no restart."""
    first = tmp_path / "one"
    second = tmp_path / "two"
    first.mkdir()
    second.mkdir()
    ctx, watcher = await _make_app(_isolated_config_home, [first])

    _write_config(_isolated_config_home, [first, second])
    await ctx.reconcile_watched_roots()

    watcher.reconfigure.assert_awaited_once()
    # The roots land on the *live* config the index engine shares, or its
    # within-roots guard would reject the very files the watcher now reports.
    assert ctx.config.indexing is watcher.reconfigure.await_args.args[0]
    assert second.resolve() in _roots(ctx)


@pytest.mark.asyncio
async def test_removed_root_stops_being_watched(
    _isolated_config_home: Path, tmp_path: Path
) -> None:
    first = tmp_path / "one"
    second = tmp_path / "two"
    first.mkdir()
    second.mkdir()
    ctx, watcher = await _make_app(_isolated_config_home, [first, second])

    _write_config(_isolated_config_home, [first])
    await ctx.reconcile_watched_roots()

    watcher.reconfigure.assert_awaited_once()
    assert _roots(ctx) == {first.resolve()}


@pytest.mark.asyncio
async def test_unchanged_config_costs_only_the_signature(
    _isolated_config_home: Path, tmp_path: Path
) -> None:
    """The per-tool-call price when nothing changed: a few stats, no parse."""
    root = tmp_path / "one"
    root.mkdir()
    ctx, watcher = await _make_app(_isolated_config_home, [root])

    with patch("memtomem.server.context.build_fresh_config") as rebuild:
        await ctx.reconcile_watched_roots()

    rebuild.assert_not_called()
    watcher.reconfigure.assert_not_awaited()


@pytest.mark.asyncio
async def test_config_edit_that_leaves_roots_alone_does_not_reconfigure(
    _isolated_config_home: Path, tmp_path: Path
) -> None:
    """A touched config file costs one parse; the watcher is left alone."""
    root = tmp_path / "one"
    root.mkdir()
    ctx, watcher = await _make_app(_isolated_config_home, [root])

    path = _isolated_config_home / ".memtomem" / "config.json"
    path.write_text(
        json.dumps({"indexing": {"memory_dirs": [str(root)], "auto_summarize": True}}),
        encoding="utf-8",
    )
    await ctx.reconcile_watched_roots()

    watcher.reconfigure.assert_not_awaited()
    # ... and the parse is not repeated on the next call.
    with patch("memtomem.server.context.build_fresh_config") as rebuild:
        await ctx.reconcile_watched_roots()
    rebuild.assert_not_called()


@pytest.mark.asyncio
async def test_broken_config_never_reads_as_root_removal(
    _isolated_config_home: Path, tmp_path: Path
) -> None:
    """``load_config_overrides`` swallows malformed JSON and hands back
    defaults. Reconciling on that would unwatch every directory the user has —
    the config read has to be strict, and a failure has to change nothing."""
    root = tmp_path / "one"
    root.mkdir()
    ctx, watcher = await _make_app(_isolated_config_home, [root])

    path = _isolated_config_home / ".memtomem" / "config.json"
    path.write_text("{ not json", encoding="utf-8")
    await ctx.reconcile_watched_roots()

    watcher.reconfigure.assert_not_awaited()
    assert ctx.config.indexing.all_index_roots() == [root]

    # The broken file is not re-parsed on every later call; fixing it changes
    # the mtime, which lets the retry through.
    with patch("memtomem.server.context.build_fresh_config") as rebuild:
        await ctx.reconcile_watched_roots()
    rebuild.assert_not_called()


@pytest.mark.asyncio
async def test_failed_reconfigure_leaves_the_previous_roots_intact(
    _isolated_config_home: Path, tmp_path: Path
) -> None:
    """Pinned from the server path, per the issue. ``reconfigure`` restores its
    own watch set, so the shared config has to be restored too — otherwise the
    engine accepts files under a directory nobody is watching."""
    first = tmp_path / "one"
    second = tmp_path / "two"
    first.mkdir()
    second.mkdir()
    ctx, watcher = await _make_app(_isolated_config_home, [first])
    watcher.reconfigure = AsyncMock(side_effect=RuntimeError("schedule boom"))

    _write_config(_isolated_config_home, [first, second])
    await ctx.reconcile_watched_roots()  # must not raise into the tool call

    assert _roots(ctx) == {first.resolve()}

    # Unbanked signature: the next call tries again rather than freezing the
    # watch set until the user happens to edit the file a second time.
    watcher.reconfigure = AsyncMock()
    await ctx.reconcile_watched_roots()
    watcher.reconfigure.assert_awaited_once()
    assert second.resolve() in _roots(ctx)


@pytest.mark.asyncio
async def test_unowned_context_never_reconciles(
    _isolated_config_home: Path, tmp_path: Path
) -> None:
    """``from_components`` contexts (CLI, tests) hold no watcher and do not own
    their components — reading the user's config into them would be a side
    effect nobody asked for."""
    root = tmp_path / "one"
    root.mkdir()
    config = Mem2MemConfig()
    config.indexing.memory_dirs = [root]
    ctx = AppContext.from_components(
        Components(
            config=config,
            storage=object(),  # type: ignore[arg-type]
            embedder=object(),  # type: ignore[arg-type]
            index_engine=object(),  # type: ignore[arg-type]
            search_pipeline=object(),  # type: ignore[arg-type]
        )
    )

    with patch("memtomem.server.context.build_fresh_config") as rebuild:
        await ctx.reconcile_watched_roots()

    rebuild.assert_not_called()


@pytest.mark.asyncio
async def test_concurrent_reconciles_reconfigure_once(
    _isolated_config_home: Path, tmp_path: Path
) -> None:
    """Two tool calls can land together; the second must see the first's work
    rather than reconfiguring the same change twice."""
    first = tmp_path / "one"
    second = tmp_path / "two"
    first.mkdir()
    second.mkdir()
    ctx, watcher = await _make_app(_isolated_config_home, [first])

    released = asyncio.Event()
    calls = 0

    async def _blocking_reconfigure(_config: object) -> None:
        nonlocal calls
        calls += 1
        await released.wait()

    watcher.reconfigure = AsyncMock(side_effect=_blocking_reconfigure)
    _write_config(_isolated_config_home, [first, second])

    a = asyncio.create_task(ctx.reconcile_watched_roots())
    b = asyncio.create_task(ctx.reconcile_watched_roots())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert calls == 1
    released.set()
    await asyncio.gather(a, b)

    assert calls == 1


@pytest.mark.asyncio
async def test_file_in_a_newly_added_root_is_indexed_without_a_restart(
    _isolated_config_home: Path, tmp_path: Path
) -> None:
    """The issue's first acceptance criterion, against a real ``FileWatcher``.

    Everything below the reconcile is real: the observer schedules the new
    root, the write raises an event, and the engine is asked to index it. Only
    the index engine is a stub — what is under test is the wiring from a config
    edit in another process to an indexed file, not the indexer.
    """
    from watchdog.observers.polling import PollingObserver

    from memtomem.indexing import watcher as watcher_mod

    monkeypatch = pytest.MonkeyPatch()
    # Poll rather than use the platform-native backend: FSEvents/inotify are
    # unavailable in some sandboxes, where a native-only test fails for reasons
    # that have nothing to do with the reconciliation under test.
    monkeypatch.setattr(
        watcher_mod, "Observer", lambda: PollingObserver(timeout=0.05), raising=False
    )
    try:
        first = tmp_path / "one"
        second = tmp_path / "two"
        first.mkdir()
        second.mkdir()

        indexed: list[Path] = []

        class _StubEngine:
            async def index_file(self, path, **_kwargs):
                indexed.append(Path(path))
                return MagicMock(mutated=False, indexed_chunks=1)

        _write_config(_isolated_config_home, [first])
        config = Mem2MemConfig()
        config.indexing.memory_dirs = [first]
        components = Components(
            config=config,
            storage=object(),  # type: ignore[arg-type]
            embedder=object(),  # type: ignore[arg-type]
            index_engine=_StubEngine(),  # type: ignore[arg-type]
            search_pipeline=None,  # type: ignore[arg-type]
        )
        ctx = AppContext(config=config)
        real_watcher = watcher_mod.FileWatcher
        with (
            patch(
                "memtomem.server.component_factory.create_components",
                return_value=components,
            ),
            patch.object(
                watcher_mod,
                "FileWatcher",
                lambda *a, **k: real_watcher(*a, debounce_ms=50, **k),
            ),
        ):
            await ctx.ensure_initialized()

        try:
            _write_config(_isolated_config_home, [first, second])
            await ctx.reconcile_watched_roots()

            (second / "added-after-startup.md").write_text("# New\n\nbody\n", encoding="utf-8")

            # Poll to a deadline rather than sleeping a fixed span: the
            # debounce plus the observer's own cadence has no bound worth
            # pinning, only a limit.
            import time as _time

            deadline = _time.monotonic() + 30.0
            while _time.monotonic() < deadline and not indexed:
                await asyncio.sleep(0.1)
            assert indexed, "the newly added root was never watched"
            assert indexed[0].name == "added-after-startup.md"
        finally:
            await ctx.close()
    finally:
        monkeypatch.undo()


@pytest.mark.asyncio
async def test_roots_reconciled_while_degraded_are_watched_after_recovery(
    _isolated_config_home: Path, tmp_path: Path
) -> None:
    """A degraded server (#2181) has a constructed but unstarted watcher.

    The tempting guard — skip reconciliation while nothing is watching — is
    wrong: recovery starts the watcher off ``_config``, so roots that arrived
    during the degraded window would be lost and the repaired server would
    watch a stale set. Reconciliation therefore has to run for an unstarted
    watcher too, which is what this pins.
    """
    from memtomem.indexing.watcher import FileWatcher

    first = tmp_path / "one"
    second = tmp_path / "two"
    first.mkdir()
    second.mkdir()

    _write_config(_isolated_config_home, [first])
    config = Mem2MemConfig()
    config.indexing.memory_dirs = [first]
    components = Components(
        config=config,
        storage=object(),  # type: ignore[arg-type]
        embedder=object(),  # type: ignore[arg-type]
        index_engine=object(),  # type: ignore[arg-type]
        search_pipeline=object(),  # type: ignore[arg-type]
    )
    components.embedding_broken = {"reason": "dim_mismatch"}
    ctx = AppContext(config=config)
    with patch(
        "memtomem.server.component_factory.create_components",
        return_value=components,
    ):
        await ctx.ensure_initialized()

    watcher = ctx._watcher
    assert isinstance(watcher, FileWatcher)
    assert ctx._watcher_started is False

    _write_config(_isolated_config_home, [first, second])
    await ctx.reconcile_watched_roots()

    # The reconcile must not have been skipped: the new root is on the config
    # the watcher will read when it starts.
    assert second.resolve() in _roots(ctx)
    assert watcher._config is ctx.config.indexing

    started_roots: list[list[Path]] = []

    async def _capture_start() -> None:
        started_roots.append(list(watcher._config.all_index_roots()))

    with patch.object(watcher, "start", _capture_start):
        await ctx.recover_from_degraded()

    assert started_roots, "recovery did not start the watcher"
    assert second.resolve() in {Path(p).expanduser().resolve() for p in started_roots[0]}


@pytest.mark.asyncio
async def test_a_tool_call_is_what_triggers_the_reconcile(
    _isolated_config_home: Path, tmp_path: Path
) -> None:
    """The reconcile rides ``ensure_initialized``'s already-initialized path,
    so every handler entry pays for it — including the ones that reach the
    context without a ``ctx``. Pinned because moving it to a single tool
    wrapper would silently narrow the coverage to whatever calls that wrapper.
    """
    first = tmp_path / "one"
    second = tmp_path / "two"
    first.mkdir()
    second.mkdir()
    ctx, watcher = await _make_app(_isolated_config_home, [first])

    _write_config(_isolated_config_home, [first, second])
    # What a handler does on entry — no direct reconcile call.
    await ctx.ensure_initialized()

    watcher.reconfigure.assert_awaited_once()
    assert second.resolve() in _roots(ctx)


@pytest.mark.asyncio
async def test_reconcile_never_writes_the_users_config(
    _isolated_config_home: Path, tmp_path: Path
) -> None:
    """Reading the config on a hot path must not rewrite it.

    ``load_config_overrides`` defaults to running the legacy ``auto_discover``
    → explicit ``memory_dirs`` migration, which persists ``config.json``. A
    tool call that rewrites the user's config as a side effect of looking at it
    is a surprise write to a file the user owns — and, under a test suite that
    does not isolate ``HOME``, a write to the developer's real config.
    """
    root = tmp_path / "one"
    root.mkdir()
    ctx, _watcher = await _make_app(_isolated_config_home, [root])

    path = _isolated_config_home / ".memtomem" / "config.json"
    # A config that would trip the migration: auto_discover on, no explicit
    # memory_dirs to replace it with.
    path.write_text(json.dumps({"indexing": {"auto_discover": True}}), encoding="utf-8")
    before = path.read_bytes()

    await ctx.reconcile_watched_roots()

    assert path.read_bytes() == before, "reconciliation rewrote the user's config file"


def _write_tiered_config(home: Path, *, memory_dirs: list[Path], project_dirs: list[Path]) -> None:
    (home / ".memtomem" / "config.json").write_text(
        json.dumps(
            {
                "indexing": {
                    "memory_dirs": [str(d) for d in memory_dirs],
                    "project_memory_dirs": [str(d) for d in project_dirs],
                }
            }
        ),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_moving_a_root_between_tiers_is_applied(
    _isolated_config_home: Path, tmp_path: Path
) -> None:
    """A directory moved from the user tier to the project tier watches the
    same paths but *is* something different: ``IndexEngine._resolve_scope``
    classifies scope off ``project_memory_dirs``, so a missed reclassification
    keeps writing project-shared content under user-tier rules. Comparing the
    flattened root list would call this "no change"."""
    root = tmp_path / "one"
    root.mkdir()
    ctx, watcher = await _make_app(_isolated_config_home, [root])

    _write_tiered_config(_isolated_config_home, memory_dirs=[], project_dirs=[root])
    await ctx.reconcile_watched_roots()

    assert [Path(d).expanduser() for d in ctx.config.indexing.memory_dirs] == []
    assert [Path(d).expanduser() for d in ctx.config.indexing.project_memory_dirs] == [root]
    watcher.reconfigure.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_reconfigure_restores_both_tiers(
    _isolated_config_home: Path, tmp_path: Path
) -> None:
    """The rollback has to put back the project tier too, not just the user
    tier — a half-restored config leaves the engine classifying scope off roots
    the watcher never accepted."""
    user_root = tmp_path / "user"
    project_root = tmp_path / "project"
    added = tmp_path / "added"
    for d in (user_root, project_root, added):
        d.mkdir()

    _write_tiered_config(
        _isolated_config_home, memory_dirs=[user_root], project_dirs=[project_root]
    )
    config = Mem2MemConfig()
    config.indexing.memory_dirs = [user_root]
    config.indexing.project_memory_dirs = [project_root]
    components = Components(
        config=config,
        storage=object(),  # type: ignore[arg-type]
        embedder=object(),  # type: ignore[arg-type]
        index_engine=object(),  # type: ignore[arg-type]
        search_pipeline=object(),  # type: ignore[arg-type]
    )
    watcher = _fake_watcher()
    ctx = AppContext(config=config)
    from memtomem.indexing import watcher as watcher_mod

    with (
        patch(
            "memtomem.server.component_factory.create_components",
            return_value=components,
        ),
        patch.object(watcher_mod, "FileWatcher", lambda *a, **k: watcher),
    ):
        await ctx.ensure_initialized()

    watcher.reconfigure = AsyncMock(side_effect=RuntimeError("schedule boom"))
    _write_tiered_config(
        _isolated_config_home, memory_dirs=[user_root], project_dirs=[project_root, added]
    )
    await ctx.reconcile_watched_roots()

    assert [Path(d).expanduser() for d in ctx.config.indexing.memory_dirs] == [user_root]
    assert [Path(d).expanduser() for d in ctx.config.indexing.project_memory_dirs] == [project_root]


@pytest.mark.asyncio
async def test_owned_context_without_a_watcher_does_not_reconcile(
    _isolated_config_home: Path, tmp_path: Path
) -> None:
    """Separate from the unowned case, so neither guard can be deleted while
    the other keeps the suite green."""
    root = tmp_path / "one"
    root.mkdir()
    ctx, _watcher = await _make_app(_isolated_config_home, [root])
    ctx._watcher = None
    assert ctx._owns_components is True

    _write_config(_isolated_config_home, [root, tmp_path])
    with patch("memtomem.server.context.build_fresh_config") as rebuild:
        await ctx.reconcile_watched_roots()

    rebuild.assert_not_called()


@pytest.mark.asyncio
async def test_unowned_context_with_a_watcher_does_not_reconcile(
    _isolated_config_home: Path, tmp_path: Path
) -> None:
    """The ownership guard has to hold on its own: a ``from_components``
    context that somehow carries a watcher still must not read the user's
    config on a tool call."""
    root = tmp_path / "one"
    root.mkdir()
    config = Mem2MemConfig()
    config.indexing.memory_dirs = [root]
    ctx = AppContext.from_components(
        Components(
            config=config,
            storage=object(),  # type: ignore[arg-type]
            embedder=object(),  # type: ignore[arg-type]
            index_engine=object(),  # type: ignore[arg-type]
            search_pipeline=object(),  # type: ignore[arg-type]
        )
    )
    ctx._watcher = _fake_watcher()  # type: ignore[assignment]

    with patch("memtomem.server.context.build_fresh_config") as rebuild:
        await ctx.reconcile_watched_roots()

    rebuild.assert_not_called()


@pytest.mark.asyncio
async def test_a_corrupted_fragment_is_not_read_as_a_removal(
    _isolated_config_home: Path, tmp_path: Path
) -> None:
    """``load_config_d`` logs and skips a malformed fragment, handing back a
    config without whatever it declared. The root here *comes from* the
    fragment, so acting on that difference would unwatch a directory because a
    file got truncated."""
    root = tmp_path / "one"
    extra = tmp_path / "two"
    root.mkdir()
    extra.mkdir()
    ctx, watcher = await _make_app(_isolated_config_home, [root])

    # Make the fragment the only declaration of the roots: config.json holds
    # no memory_dirs, so an override cannot replace what the fragment sets.
    (_isolated_config_home / ".memtomem" / "config.json").write_text("{}", encoding="utf-8")
    frag_dir = _isolated_config_home / ".memtomem" / "config.d"
    frag_dir.mkdir(exist_ok=True)
    frag = frag_dir / "10-roots.json"
    frag.write_text(
        json.dumps({"indexing": {"memory_dirs": [str(root), str(extra)]}}), encoding="utf-8"
    )
    await ctx.reconcile_watched_roots()
    from_fragment = _roots(ctx)
    assert extra.resolve() in from_fragment, "the fragment should be the source of this root"

    # Now truncate it. A lenient load would report the fragment's roots as
    # gone and unwatch them; the strict read refuses the whole rebuild.
    watcher.reconfigure.reset_mock()
    frag.write_text("{ truncated", encoding="utf-8")
    await ctx.reconcile_watched_roots()

    watcher.reconfigure.assert_not_awaited()
    assert _roots(ctx) == from_fragment
