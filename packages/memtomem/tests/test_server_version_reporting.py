"""Regression for #383: MCP ``serverInfo.version`` must be the memtomem
package version, not the transport SDK's.

Left unset, the value the lowlevel server puts on the wire is not the
package's: 1.x substituted ``importlib.metadata.version("mcp")`` — the SDK's
own version, which is what #383 was filed about — and 2.0 substitutes an
empty string. Either leaks to every ``initialize`` response as
``serverInfo.version``, where external consumers (monitoring, client
telemetry, error reports) read it as ours.

Both tests below lock in the fix from #383:

* A unit test asserts ``mcp.version`` matches ``memtomem.__version__``
  at import time — the ``version=`` constructor argument applies
  unconditionally during module construction.
* An end-to-end test drives the ``initialize`` RPC against a real
  subprocess and parses the JSON-RPC response, so a regression that
  bypasses it (e.g. a future SDK release that resets ``.version``
  during ``run``) is still caught.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import memtomem
from memtomem.server import mcp


def test_server_version_matches_package_version() -> None:
    """Unit: ``mcp.version`` is pinned at import time."""
    assert mcp.version == memtomem.__version__, (
        "serverInfo.version must track memtomem.__version__, not the "
        "MCP SDK version; see the ``version=`` argument in "
        "memtomem/server/__init__.py"
    )


@pytest.mark.skipif(sys.platform == "win32", reason="server is POSIX-only")
def test_initialize_response_reports_memtomem_version(tmp_path: Path) -> None:
    """End-to-end: drive the ``initialize`` RPC and assert
    ``serverInfo.version`` round-trips the memtomem package version.

    Isolates ``HOME`` + ``XDG_RUNTIME_DIR`` under ``tmp_path`` so the
    server doesn't touch the developer's real state during the probe.
    """
    home = tmp_path / "home"
    home.mkdir()
    xdg = tmp_path / "xdg_runtime"
    xdg.mkdir()
    os.chmod(xdg, 0o700)

    env = os.environ.copy()
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
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

        # Read the first JSON-RPC line the server emits. Cold imports can
        # take up to ~10s on slow CI hosts; cap the read with a deadline so
        # a hung server fails loud.
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
        server_info = response.get("result", {}).get("serverInfo", {})
        assert server_info.get("name") == "memtomem"
        assert server_info.get("version") == memtomem.__version__, (
            f"serverInfo.version must be the memtomem package version "
            f"({memtomem.__version__}), got {server_info.get('version')!r}. "
            f"An empty string (or an MCP SDK version) means the ``version=`` "
            f"argument in server/__init__.py stopped reaching the wire."
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
