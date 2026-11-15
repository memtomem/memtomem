"""Every resource answers through a real stdio connection (#3374).

A resource handler takes no ``ctx``, so it finds the connection's
``AppContext`` through a context variable. Which task's context a handler runs
in is the SDK's decision, and only a real transport shows it: over stdio the
handler runs in a copy of the stdin reader's context, taken before the server's
lifespan has run. Calling the handler functions, or the server object's
``read_resource``, from the test's own task cannot tell a working binding from
one that only that task can see.

These are also the tests that read the templated resources with nothing
patched. The in-process tests that read them replace the helper that finds
the ``AppContext``, which is how a templated handler could be handed a
``Context`` without its request and still pass them.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from .test_context_writes_off_mcp import _server_env, _server_session

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="server is POSIX-only")


async def _read_json(session: object, uri: str) -> object:
    result = await session.read_resource(uri)  # type: ignore[attr-defined]
    assert len(result.contents) == 1, result
    return json.loads(result.contents[0].text)


async def test_static_resources_read_over_stdio(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    env = _server_env(tmp_path, home, "core", None)
    env["MEMTOMEM_STORAGE__SQLITE_PATH"] = str(tmp_path / "store.db")
    # No model to fetch: the resources read the store, not the embedder.
    env["MEMTOMEM_EMBEDDING__PROVIDER"] = "none"

    async with _server_session(env, cwd) as session:
        # A tool call first: the server is up and its store is open, so a
        # failing read below is the resource path's own.
        stats_tool = await session.call_tool("mem_stats", {})
        assert not stats_tool.is_error, stats_tool

        assert await _read_json(session, "memtomem://stats") == {
            "total_chunks": 0,
            "total_sources": 0,
        }
        assert await _read_json(session, "memtomem://namespaces") == []
        assert await _read_json(session, "memtomem://tags") == []
        assert await _read_json(session, "memtomem://sources") == {
            "sources": [],
            "page_size": 50,
            "next": None,
        }


async def test_templated_resources_read_over_stdio(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    env = _server_env(tmp_path, home, "core", None)
    env["MEMTOMEM_STORAGE__SQLITE_PATH"] = str(tmp_path / "store.db")
    env["MEMTOMEM_EMBEDDING__PROVIDER"] = "none"

    absent = "00000000-0000-4000-8000-000000000000"
    async with _server_session(env, cwd) as session:
        # ``page_size`` echoes the template variable, so the answer is this
        # template's and not the static first page.
        assert await _read_json(session, "memtomem://sources?limit=5") == {
            "sources": [],
            "page_size": 5,
            "next": None,
        }
        assert await _read_json(session, f"memtomem://chunks/{absent}") == {
            "error": f"Chunk {absent} not found"
        }


async def test_static_resource_is_the_first_request_over_stdio(tmp_path: Path) -> None:
    """Nothing has opened the store yet, so the read initializes it itself."""
    home = tmp_path / "home"
    home.mkdir()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    env = _server_env(tmp_path, home, "core", None)
    env["MEMTOMEM_STORAGE__SQLITE_PATH"] = str(tmp_path / "store.db")
    env["MEMTOMEM_EMBEDDING__PROVIDER"] = "none"

    async with _server_session(env, cwd) as session:
        assert await _read_json(session, "memtomem://namespaces") == []
