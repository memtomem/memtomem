"""End-to-end acceptance for the Phase 3 lazy-init flip (#399).

Phase 3 moved component construction (and watcher / scheduler /
watchdog start) out of ``app_lifespan`` and into the first-tool-call
path. The behaviour we promise the user is:

1. An MCP client connecting and not making any tool call (the
   ``claude mcp list`` health-check shape) leaves ``~/.memtomem/`` alone.
2. The first tool call triggers init and the DB appears.
3. Concurrent first calls share a single init pass — they don't race
   into duplicate ``create_components`` invocations.

These tests drive the real ``app_lifespan`` against a tmp-isolated
``HOME`` so the storage path resolves under the test directory rather
than the user's machine. ``embedding.provider=none`` keeps init fast
(NoopEmbedder, no model download), and ``indexing.memory_dirs=[]``
keeps the file watcher from picking up the contributor's actual
notes via auto-discovery while the test runs.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from memtomem.server import lifespan as lifespan_mod


@pytest.fixture
def isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Redirect storage + auto-discover roots to ``tmp_path``.

    ``HOME`` swap covers the ``~/.memtomem/config.json`` read path
    (`feedback_config_json_isolation.md`) so we don't pick up real
    fragments. The explicit ``MEMTOMEM_STORAGE__SQLITE_PATH`` puts the
    DB at a known location for assertions, and the empty
    ``MEMTOMEM_INDEXING__MEMORY_DIRS`` keeps the watcher from scanning
    the developer's actual notes when it starts under default config.
    """
    home = tmp_path / "home"
    home.mkdir()
    db_path = tmp_path / "memtomem.db"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("MEMTOMEM_STORAGE__SQLITE_PATH", str(db_path))
    monkeypatch.setenv("MEMTOMEM_INDEXING__MEMORY_DIRS", "[]")
    monkeypatch.setenv("MEMTOMEM_EMBEDDING__PROVIDER", "none")
    # Belt-and-braces: keep every optional subsystem off so this test
    # exercises only the watcher path that lazy-init owns now.
    monkeypatch.setenv("MEMTOMEM_CONSOLIDATION_SCHEDULE__ENABLED", "false")
    monkeypatch.setenv("MEMTOMEM_POLICY__ENABLED", "false")
    monkeypatch.setenv("MEMTOMEM_HEALTH_WATCHDOG__ENABLED", "false")
    monkeypatch.setenv("MEMTOMEM_WEBHOOK__ENABLED", "false")
    return {"home": home, "db_path": db_path}


@pytest.mark.asyncio
async def test_handshake_only_leaves_db_absent(isolated_state: dict[str, Path]) -> None:
    """Lifespan enter → exit without any tool call must not create the DB.

    This is the user-visible regression #399 fixes: previously, every
    MCP client that connected (Claude Code's ``claude mcp list``,
    Cursor, Windsurf, Gemini CLI) instantiated ``~/.memtomem/memtomem.db``
    on handshake even before the user ran ``mm init``. Phase 3 moves
    the DB open into the first tool-call path, so a handshake-only
    session leaves the storage path alone.
    """
    db_path = isolated_state["db_path"]

    async with lifespan_mod.app_lifespan(MagicMock()) as ctx:
        assert ctx is not None
        # Components are unset → storage was never opened.
        assert ctx._components is None

    assert not db_path.exists(), (
        f"Phase 3 invariant: handshake-only session must leave the DB "
        f"untouched, but {db_path} exists after lifespan exit"
    )


@pytest.mark.asyncio
async def test_first_ensure_initialized_creates_db(isolated_state: dict[str, Path]) -> None:
    """The first tool-call path (modeled here as a direct
    ``ctx.ensure_initialized()``) opens storage and creates the DB.

    This is the inverse of the handshake-only invariant — together they
    pin that the DB lifecycle now follows tool calls, not the lifespan.
    """
    db_path = isolated_state["db_path"]

    async with lifespan_mod.app_lifespan(MagicMock()) as ctx:
        assert not db_path.exists(), "DB must not exist before first tool call"

        await ctx.ensure_initialized()

        assert db_path.exists(), f"first ensure_initialized must create the DB at {db_path}"
        assert ctx._components is not None


@pytest.mark.asyncio
async def test_concurrent_first_calls_invoke_create_components_once(
    isolated_state: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two tool handlers arriving simultaneously on a fresh server must
    share a single ``create_components`` pass — the ``_init_lock``
    serializes them. A regression where the lock is dropped (or where a
    handler bypasses ``ensure_initialized``) would let two factories
    race on the same SQLite file."""
    from memtomem.server import component_factory

    real_create = component_factory.create_components
    call_count = 0

    async def counting_create(config: object) -> object:
        nonlocal call_count
        call_count += 1
        # Tiny sleep widens the race window so a missing lock surfaces
        # reliably across CI runners.
        await asyncio.sleep(0.01)
        return await real_create(config)  # type: ignore[arg-type]

    monkeypatch.setattr(component_factory, "create_components", counting_create)

    async with lifespan_mod.app_lifespan(MagicMock()) as ctx:
        results = await asyncio.gather(
            ctx.ensure_initialized(),
            ctx.ensure_initialized(),
            ctx.ensure_initialized(),
        )

    assert call_count == 1, (
        f"_init_lock must serialise concurrent first-callers; "
        f"create_components ran {call_count}× instead of once"
    )
    assert results[0] is results[1] is results[2], (
        "all concurrent callers must observe the same Components instance"
    )
