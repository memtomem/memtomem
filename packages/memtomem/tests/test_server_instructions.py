"""Pin tests for the FastMCP ``instructions=`` field.

The instructions string set in ``memtomem/server/__init__.py`` is
auto-injected into every MCP client's ``initialize`` response. For most
LLMs this is the only workflow-level signal they get before deciding
which memtomem tool to call — drift here means clients silently fall
back to docstring-only inference and pick the wrong tool (e.g. plain
``mem_add`` when the user asked for per-agent isolation).

Two layers, mirroring ``test_server_version_reporting.py``:

* A unit test asserts the constant is wired through to
  ``mcp.instructions`` and mentions every workflow token an LLM has to
  recognize. If a tool is renamed or a namespace convention changes,
  both ``server/instructions.py`` and the ``REQUIRED_TOKENS`` tuple
  below must move in lockstep.
* An end-to-end test drives the ``initialize`` RPC against a real
  subprocess and asserts the ``instructions`` field round-trips, so a
  future FastMCP release that drops the parameter (or strips it during
  startup) is still caught.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from memtomem.server import mcp
from memtomem.server.instructions import INSTRUCTIONS

REQUIRED_TOKENS: tuple[str, ...] = (
    # Single-agent quickstart — what 90% of users should reach for.
    "mem_add",
    "mem_search",
    # Multi-agent workflow — the recipe an LLM has to follow in order.
    "mem_agent_register",
    "mem_session_start",
    "mem_agent_search",
    "mem_agent_share",
    "mem_session_end",
    # Namespace conventions — the vocabulary the LLM needs to recognize.
    "agent-runtime:",
    "shared:",
    "default",
)


def test_instructions_is_wired_to_fastmcp() -> None:
    """The constant from ``server/instructions.py`` reaches
    ``mcp.instructions`` (and therefore the ``initialize`` response).
    Without this, MCP clients have nothing but tool docstrings to go on.
    """
    assert mcp.instructions == INSTRUCTIONS, (
        "FastMCP(instructions=...) must be passed the INSTRUCTIONS "
        "constant verbatim; see memtomem/server/__init__.py"
    )


@pytest.mark.parametrize("token", REQUIRED_TOKENS)
def test_instructions_mentions_workflow_token(token: str) -> None:
    """Each token names a tool or convention an LLM must recognize to
    route a user request correctly. If a tool is renamed, update both
    ``INSTRUCTIONS`` and ``REQUIRED_TOKENS`` so this pin keeps working.
    """
    assert token in INSTRUCTIONS, (
        f"instructions string lost reference to {token!r}; if the "
        f"workflow changed, update memtomem/server/instructions.py "
        f"and the REQUIRED_TOKENS tuple in lockstep."
    )


@pytest.mark.skipif(sys.platform == "win32", reason="server is POSIX-only")
def test_initialize_response_carries_instructions(tmp_path: Path) -> None:
    """End-to-end: drive the ``initialize`` RPC and assert the
    ``instructions`` field on the response matches the wired constant.

    Isolates ``HOME`` + ``XDG_RUNTIME_DIR`` under ``tmp_path`` so the
    server doesn't touch the developer's real state during the probe
    (mirrors ``test_server_version_reporting.py``).
    """
    home = tmp_path / "home"
    home.mkdir()
    xdg = tmp_path / "xdg_runtime"
    xdg.mkdir()
    os.chmod(xdg, 0o700)

    env = os.environ.copy()
    env["HOME"] = str(home)
    env["XDG_RUNTIME_DIR"] = str(xdg)

    initialize_request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test-probe", "version": "0.1"},
        },
    }

    proc = subprocess.Popen(
        [sys.executable, "-m", "memtomem.server"],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write((json.dumps(initialize_request) + "\n").encode())
        proc.stdin.flush()

        deadline = time.monotonic() + 15
        response_line: bytes | None = None
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                if proc.poll() is not None:
                    stderr = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
                    pytest.fail(
                        f"Server exited before responding (rc={proc.returncode}). stderr:\n{stderr}"
                    )
                continue
            response_line = line
            break
        if response_line is None:
            stderr = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
            pytest.fail(f"No initialize response within 15s. stderr:\n{stderr}")

        response = json.loads(response_line)
        result = response.get("result", {})
        instructions = result.get("instructions")
        assert instructions == INSTRUCTIONS, (
            "initialize response's `instructions` field must round-trip "
            "the wired constant; if this fails, FastMCP may have dropped "
            "the parameter or stripped it during startup."
        )
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
