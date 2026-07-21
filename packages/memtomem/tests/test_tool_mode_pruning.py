"""Per-mode tool-surface pins for ``MEMTOMEM_TOOL_MODE`` (#1609).

Pruning in ``server/__init__.py`` couples to two FastMCP surfaces:
``ToolManager.list_tools`` (to enumerate) and ``FastMCP.remove_tool``
(to prune). A version bump that renames or restructures either would
break pruning **silently** — the server would start in effective
``full`` mode with no error. These tests are the canary:

* An in-process check asserts the guarded enumeration helper and the
  public ``remove_tool`` API are both present and shaped as expected.
* Subprocess checks drive the ``tools/list`` RPC under each
  ``MEMTOMEM_TOOL_MODE`` and assert the EXACT exposed tool set, computed
  independently from ``_CORE_TOOLS`` + the ``_STANDARD_PACKS`` registry
  slice (mirrors ``test_server_instructions.py``'s harness).
"""

from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import time
from pathlib import Path

import pytest

from memtomem.server import (
    _ALL_REGISTERED_TOOLS,
    _CORE_TOOLS,
    _STANDARD_PACKS,
    mcp,
)
from memtomem.server.tool_registry import ACTIONS


def _expected_tools(mode: str) -> set[str]:
    """Independently compute the tool set a mode should expose — the
    same derivation the pruning uses, so a drift in either fails here."""
    if mode == "full":
        return set(_ALL_REGISTERED_TOOLS)
    if mode == "standard":
        pack_tools = {
            f"mem_{name}" for name, info in ACTIONS.items() if info.category in _STANDARD_PACKS
        }
        return set(_CORE_TOOLS) | (pack_tools & set(_ALL_REGISTERED_TOOLS))
    return set(_CORE_TOOLS)


def test_coupled_fastmcp_apis_present() -> None:
    """The two FastMCP coupling points pruning relies on must exist and
    be shaped as expected. If this fails after an ``mcp`` bump, the
    pruning logic in ``server/__init__.py`` needs updating — do not just
    relax this test."""
    manager = getattr(mcp, "_tool_manager", None)
    assert manager is not None, "FastMCP no longer exposes _tool_manager"
    assert callable(getattr(manager, "list_tools", None)), (
        "ToolManager.list_tools is gone — tool enumeration for pruning will break"
    )
    assert callable(getattr(mcp, "remove_tool", None)), (
        "FastMCP.remove_tool is gone — tool pruning will break"
    )
    # list_tools must yield objects carrying a ``.name`` we can key on.
    names = [t.name for t in manager.list_tools()]
    assert "mem_do" in names


def test_core_is_nine_tools() -> None:
    assert len(_CORE_TOOLS) == 9


def _tools_list_under_mode(mode: str, tmp_path: Path) -> set[str]:
    """Spawn the server with ``MEMTOMEM_TOOL_MODE=mode`` and return the
    tool names it advertises via the ``tools/list`` RPC.

    Isolates HOME/XDG under ``tmp_path`` (mirrors
    ``test_server_instructions.py``) so the probe never touches real
    developer state.
    """
    home = tmp_path / "home"
    home.mkdir()
    xdg = tmp_path / "xdg"
    xdg.mkdir()
    os.chmod(xdg, 0o700)

    env = os.environ.copy()
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env["XDG_RUNTIME_DIR"] = str(xdg)
    env["MEMTOMEM_TOOL_MODE"] = mode

    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "prune-probe", "version": "0.1"},
        },
    }
    initialized = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    list_req = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}

    proc = subprocess.Popen(
        [sys.executable, "-m", "memtomem.server"],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert proc.stdin is not None and proc.stdout is not None
        for msg in (init, initialized, list_req):
            proc.stdin.write((json.dumps(msg) + "\n").encode())
        proc.stdin.flush()

        deadline = time.monotonic() + 20
        tools: set[str] | None = None
        stdout_fd = proc.stdout.fileno()
        buf = b""
        while tools is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            # select + nonblocking os.read (never buffered readline): a
            # readable fd only guarantees *some* bytes, so readline could
            # still block on a partial line without a newline. Accumulate
            # raw bytes and split complete frames ourselves so no read
            # path can outlive the deadline (POSIX-only test).
            ready, _, _ = select.select([stdout_fd], [], [], remaining)
            if not ready:
                if proc.poll() is not None:
                    stderr = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
                    pytest.fail(f"server exited early (rc={proc.returncode}). stderr:\n{stderr}")
                continue
            chunk = os.read(stdout_fd, 65536)
            if not chunk:  # EOF
                if proc.poll() is not None:
                    stderr = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
                    pytest.fail(f"server exited early (rc={proc.returncode}). stderr:\n{stderr}")
                continue
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                try:
                    resp = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if resp.get("id") == 2 and "result" in resp:
                    tools = {t["name"] for t in resp["result"]["tools"]}
                    break
        if tools is None:
            # Deadline hit with the server still alive and silent — kill
            # first, THEN drain stderr. Reading an open stderr pipe on a
            # live process would itself block forever, defeating the very
            # timeout this path exists to enforce (Codex review).
            proc.kill()
            try:
                _, stderr_b = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                stderr_b = b""
            pytest.fail(
                f"no tools/list response within 20s. stderr:\n{stderr_b.decode(errors='replace')}"
            )
        return tools
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass


@pytest.mark.skipif(sys.platform == "win32", reason="server is POSIX-only")
@pytest.mark.parametrize("mode", ["core", "standard", "full"])
def test_tools_list_matches_expected_set(mode: str, tmp_path: Path) -> None:
    """End-to-end: the tool set advertised in each mode is EXACTLY the
    independently-derived expected set. Catches both a pruning
    regression (wrong tools removed) and a silent FastMCP-API break
    (nothing removed → full surface in core mode)."""
    advertised = _tools_list_under_mode(mode, tmp_path)
    expected = _expected_tools(mode)
    assert advertised == expected, (
        f"{mode} mode advertised {sorted(advertised - expected)} extra / "
        f"{sorted(expected - advertised)} missing vs expected"
    )


@pytest.mark.skipif(sys.platform == "win32", reason="server is POSIX-only")
def test_core_mode_prunes_multi_agent_tools(tmp_path: Path) -> None:
    """Regression guard for the #1608/#1609 class: the multi-agent and
    session tools the instructions route through ``mem_do`` must NOT be
    individually exposed in the default core mode."""
    advertised = _tools_list_under_mode("core", tmp_path)
    for pruned in (
        "mem_agent_register",
        "mem_agent_search",
        "mem_agent_share",
        "mem_session_start",
        "mem_batch_add",
    ):
        assert pruned not in advertised, f"{pruned} must be pruned in core mode"
    assert "mem_do" in advertised
