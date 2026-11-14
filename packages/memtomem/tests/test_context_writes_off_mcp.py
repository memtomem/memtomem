"""Context writes have no MCP route: CLI (and, for some, the web UI) only.

Every ``mm context`` operation that writes (init, generate, sync, pull, memory
and artifact migration, transfer, versions and labels) reaches the agents' own
configuration: hook settings, skills, instruction files. Over MCP the caller
is an agent, and a consent flag it passes as an argument (``allow_host_writes``,
``confirm_project_shared``, ``apply``) is not consent from the person. So none
of them is an MCP tool in any mode, a ``mem_do`` action, or a name
``MEMTOMEM_EXTRA_TOOLS`` can expose. ``mem_context_detect`` and
``mem_context_diff`` read only and stay.

The in-process pins name every removed action, so re-registering one fails
here. The subprocess pins drive the real server over stdio, because the
registry and the advertised surface are two places that can disagree.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING

import anyio
import pytest

from memtomem.server import _ALL_REGISTERED_TOOLS, _CORE_TOOLS, _STANDARD_PACKS
from memtomem.server.tool_registry import ACTIONS
from memtomem.server.tools.meta import _ALIASES, mem_do

if TYPE_CHECKING:
    from mcp import ClientSession

#: The context operations that write. None may be reachable over MCP.
REMOVED_ACTIONS = (
    "context_init",
    "context_generate",
    "context_sync",
    "context_memory_migrate",
    "context_artifact_migrate",
    "context_artifact_transfer",
    "context_version",
    "context_promote",
    "context_pull",
)
REMOVED_TOOLS = tuple(f"mem_{name}" for name in REMOVED_ACTIONS)

#: ``mem_do`` aliases that used to route into the removed set.
FORMER_ALIASES = ("artifact_migrate", "promote")

#: The context-named tools that stay: two reads, and the pinned-context
#: composer (category "pinned", read only).
RETAINED_CONTEXT_TOOLS = frozenset(
    {"mem_context_detect", "mem_context_diff", "mem_context_compose"}
)

#: Bounds every wait on the child server: one response, and one whole test.
_READ_TIMEOUT_S = 30.0
_TEST_BUDGET_S = 90.0


def _text(result: object) -> str:
    return " ".join(getattr(c, "text", "") for c in getattr(result, "content", []))


def test_registered_context_actions_are_the_two_reads() -> None:
    registered = {name for name, info in ACTIONS.items() if info.category == "context"}
    assert registered == {"context_detect", "context_diff"}


def test_no_removed_action_is_registered_under_any_category() -> None:
    assert set(REMOVED_ACTIONS) & set(ACTIONS) == set()


def test_registered_context_tools_are_exactly_the_retained_set() -> None:
    registered = {name for name in _ALL_REGISTERED_TOOLS if name.startswith("mem_context_")}
    assert registered == RETAINED_CONTEXT_TOOLS


def test_no_alias_routes_into_the_removed_set() -> None:
    assert set(FORMER_ALIASES) & set(_ALIASES) == set()
    assert set(_ALIASES.values()) & set(REMOVED_ACTIONS) == set()


@pytest.mark.parametrize("action", REMOVED_ACTIONS + FORMER_ALIASES)
async def test_mem_do_answers_unknown_action(
    action: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Run from a throwaway project: were the action registered again, the
    # call would generate agent files in whatever project the cwd resolves to.
    proj = tmp_path / "proj"
    (proj / ".git").mkdir(parents=True)
    monkeypatch.chdir(proj)
    out = await mem_do(action=action, params={})
    assert out.startswith(f"Error: unknown action '{action}'."), out
    assert sorted(p.name for p in proj.iterdir()) == [".git"]


# ── the real server over stdio ────────────────────────────────────────────────


def _server_env(tmp_path: Path, home: Path, mode: str, extra_tools: str | None) -> dict[str, str]:
    """The child's environment, isolated from the developer's own state.

    ``HOME`` and the runtime directory point into ``tmp_path``. The server also
    loads a ``.env`` it finds by walking up from its own source file, which under
    an editable install reaches the checkout root whatever ``HOME`` and the cwd
    are; ``PYTHON_DOTENV_DISABLED`` closes that. ``PYTHONPATH`` entries are made
    absolute because the child runs from the project directory.
    """
    xdg = tmp_path / "xdg"
    xdg.mkdir(exist_ok=True)
    os.chmod(xdg, 0o700)
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "XDG_RUNTIME_DIR": str(xdg),
            "MEMTOMEM_TOOL_MODE": mode,
            "PYTHON_DOTENV_DISABLED": "1",
        }
    )
    env.pop("MEMTOMEM_EXTRA_TOOLS", None)
    if extra_tools is not None:
        env["MEMTOMEM_EXTRA_TOOLS"] = extra_tools
    if env.get("PYTHONPATH"):
        env["PYTHONPATH"] = os.pathsep.join(
            str(Path(p).resolve()) for p in env["PYTHONPATH"].split(os.pathsep) if p
        )
    return env


@contextlib.asynccontextmanager
async def _server_session(env: dict[str, str], cwd: Path) -> AsyncIterator["ClientSession"]:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=sys.executable, args=["-m", "memtomem.server"], env=env, cwd=str(cwd)
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write, read_timeout_seconds=_READ_TIMEOUT_S) as session:
            await session.initialize()
            yield session


def _expected_tools(mode: str) -> set[str]:
    if mode == "standard":
        packs = {f"mem_{n}" for n, info in ACTIONS.items() if info.category in _STANDARD_PACKS}
        return set(_CORE_TOOLS) | (packs & set(_ALL_REGISTERED_TOOLS))
    return set(_CORE_TOOLS)


@pytest.mark.skipif(sys.platform == "win32", reason="server is POSIX-only")
@pytest.mark.parametrize("mode", ["core", "standard"])
async def test_extra_tools_env_cannot_expose_a_removed_tool(mode: str, tmp_path: Path) -> None:
    """``MEMTOMEM_EXTRA_TOOLS`` adds only registered names; the removed ones are
    not registered, so naming them adds nothing and the mode's own set stays."""
    home = tmp_path / "home"
    home.mkdir()
    env = _server_env(tmp_path, home, mode, ",".join(REMOVED_TOOLS))
    with anyio.fail_after(_TEST_BUDGET_S):
        async with _server_session(env, tmp_path) as session:
            advertised = {t.name for t in (await session.list_tools()).tools}
    assert advertised == _expected_tools(mode)
    assert set(_CORE_TOOLS) <= advertised


_MARKER = "echo CONTEXT-WRITE-MARKER"


def _project_with_a_settings_hook(tmp_path: Path) -> tuple[Path, Path]:
    """A project that ships a ``SessionStart`` hook in ``.memtomem/settings.json``,
    a canonical skill, and empty runtime directories, beside a ``HOME`` that has
    only ``.claude/``. Against a server that still registered the context writes,
    ``mem_do("context_sync", include="settings,skills", scope="user",
    allow_host_writes=True)`` from this project wrote the hook into
    ``~/.claude/settings.json``, ``~/.codex/hooks.json`` and
    ``~/.gemini/settings.json``."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    proj = tmp_path / "proj"
    (proj / ".git").mkdir(parents=True)
    (proj / ".memtomem").mkdir()
    for runtime in (".claude", ".codex", ".gemini"):
        (proj / runtime).mkdir()
    (proj / ".memtomem" / "settings.json").write_text(
        json.dumps(
            {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": _MARKER}]}]}}
        ),
        encoding="utf-8",
    )
    skill = proj / ".memtomem" / "skills" / "probe"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: probe\ndescription: probe\n---\n\nprobe body\n", encoding="utf-8"
    )
    return home, proj


#: Where the runtimes read user-level hooks, skills and instructions from.
_RUNTIME_HOMES = (".claude", ".claude.json", ".codex", ".gemini", ".agents", ".kimi-code")


def _runtime_home_files(home: Path) -> dict[str, str]:
    """Every file under the runtime homes, by path relative to ``home``. The
    server's own state under ``~/.memtomem`` is not part of the comparison."""
    files: dict[str, str] = {}
    for top in _RUNTIME_HOMES:
        root = home / top
        candidates = [root] if root.is_file() else sorted(root.rglob("*"))
        for p in candidates:
            if p.is_file():
                files[str(p.relative_to(home))] = p.read_text(encoding="utf-8", errors="replace")
    return files


@pytest.mark.skipif(sys.platform == "win32", reason="server is POSIX-only")
async def test_full_mode_server_refuses_every_removed_route(tmp_path: Path) -> None:
    """Full mode advertises every registered tool, so it is the widest surface.
    Each removed tool is absent from ``tools/list`` and refused as an unknown
    tool; each removed action and former alias is refused by ``mem_do`` as an
    unknown action; and nothing lands in the runtime homes under ``HOME`` from
    the project's hook or skill."""
    home, proj = _project_with_a_settings_hook(tmp_path)
    env = _server_env(tmp_path, home, "full", None)
    before = _runtime_home_files(home)

    with anyio.fail_after(_TEST_BUDGET_S):
        async with _server_session(env, proj) as session:
            advertised = {t.name for t in (await session.list_tools()).tools}
            assert advertised & set(REMOVED_TOOLS) == set()
            assert {"mem_context_detect", "mem_context_diff"} <= advertised

            # The settings fan-out with every consent flag set, as an agent would.
            sync_args = {"include": "settings,skills", "scope": "user", "allow_host_writes": True}
            for tool in REMOVED_TOOLS:
                args = sync_args if tool == "mem_context_sync" else {}
                direct = await session.call_tool(tool, args)
                assert direct.is_error, tool
                assert f"unknown tool: {tool}" in _text(direct).casefold(), (tool, _text(direct))

            for action in REMOVED_ACTIONS + FORMER_ALIASES:
                params_ = sync_args if action == "context_sync" else {}
                res = await session.call_tool("mem_do", {"action": action, "params": params_})
                assert f"unknown action '{action}'" in _text(res), (action, _text(res))

            # The retained read still answers, so the refusals above are the
            # surface's, not a broken session's.
            diff = await session.call_tool("mem_do", {"action": "context_diff", "params": {}})
            assert not diff.is_error
            assert "unknown action" not in _text(diff)

    after = _runtime_home_files(home)
    assert after == before
    assert not any(_MARKER in body for body in after.values())
