"""``mm serve`` really starts the MCP stdio server in a subprocess.

This has to be an end-to-end subprocess test, not a mock. The failure it
guards is an argv collision that only exists across the Click/argparse
boundary: ``memtomem.server.main`` re-parses the command line with argparse,
and Click does not remove ``serve`` from ``sys.argv``. A test that patched
``server.main`` and asserted it was called would pass while the real command
died with ``unrecognized arguments: serve`` and exit code 2.

The registry record depends on this path: a client launches
``uvx memtomem`` with the record's ``packageArguments`` appended, so ``serve``
is what actually starts the server for every registry-driven install.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parents[3]
# The console script beside the running interpreter — the same file
# ``uvx memtomem`` resolves to. ``python -m memtomem.cli`` is not a substitute:
# the package has no ``__main__``, so it cannot be executed that way at all.
_MM = Path(sys.executable).parent / ("memtomem.exe" if os.name == "nt" else "memtomem")
_DEADLINE_S = 90.0
_INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "serve-e2e", "version": "0"},
    },
}


def _env(home: Path) -> dict[str, str]:
    """An isolated HOME so the probe never reads or writes the real store."""
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env["MEMTOMEM_STORAGE__SQLITE_PATH"] = str(home / "probe.db")
    # This field is a list: pydantic-settings parses it as JSON, so a bare
    # path raises SettingsError before the server ever starts.
    env["MEMTOMEM_INDEXING__MEMORY_DIRS"] = json.dumps([str(home / "notes")])
    env.pop("MEMTOMEM_TOOL_MODE", None)
    return env


def _talk(
    argv: list[str], home: Path, payloads: list[dict], expect_ids: set[int]
) -> tuple[dict[int, dict], str]:
    """Send ``payloads``, read until every id in ``expect_ids`` has answered.

    stdin stays open until then. Writing the requests and closing stdin in one
    shot races the server's own shutdown-on-EOF: ``initialize`` usually lands
    but ``tools/list`` may not, which made an earlier version of this test
    pass or fail run to run. Reading until the responses arrive removes the
    race instead of sleeping past it.
    """
    (home / "notes").mkdir(parents=True, exist_ok=True)
    seen: dict[int, dict] = {}
    lines: list[str] = []
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=_ROOT,
        env=_env(home),
    )
    assert proc.stdin and proc.stdout

    def _read() -> None:
        for line in proc.stdout:  # type: ignore[union-attr]
            lines.append(line)
            stripped = line.strip()
            if not stripped.startswith("{"):
                continue
            try:
                message = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(message.get("id"), int):
                seen[message["id"]] = message
                if expect_ids <= seen.keys():
                    return

    reader = threading.Thread(target=_read, daemon=True)
    reader.start()
    try:
        for payload in payloads:
            proc.stdin.write(json.dumps(payload) + "\n")
        proc.stdin.flush()
        reader.join(timeout=_DEADLINE_S)
    finally:
        proc.kill()
        proc.wait(timeout=30)
    return seen, "".join(lines)


def _stderr_of(argv: list[str], home: Path) -> subprocess.CompletedProcess[str]:
    """For the cases that must fail before any protocol exchange."""
    (home / "notes").mkdir(parents=True, exist_ok=True)
    return subprocess.run(
        argv, input="", capture_output=True, text=True, timeout=120, cwd=_ROOT, env=_env(home)
    )


pytestmark = pytest.mark.skipif(not _MM.exists(), reason=f"console script not installed at {_MM}")


class TestServeStartsTheStdioServer:
    def test_serve_answers_initialize(self, tmp_path: Path) -> None:
        """The whole point: ``serve`` reaches the server rather than argparse."""
        responses, out = _talk([str(_MM), "serve"], tmp_path, [_INITIALIZE], {1})

        assert 1 in responses, f"no initialize response.\nstdout={out[:500]}"
        assert "result" in responses[1]
        assert "capabilities" in responses[1]["result"]

    def test_serve_exposes_the_core_tools(self, tmp_path: Path) -> None:
        """A registry-driven client must find tools, not an empty server."""
        responses, out = _talk(
            [str(_MM), "serve"],
            tmp_path,
            [
                _INITIALIZE,
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            ],
            {1, 2},
        )

        assert 2 in responses, f"no tools/list response.\nstdout={out[:500]}"
        names = {tool["name"] for tool in responses[2]["result"]["tools"]}
        # Default tool mode is "core"; mem_search / mem_add are the two the
        # server's own instructions tell a client to start with.
        assert {"mem_search", "mem_add"} <= names, sorted(names)

    def test_serve_is_stdio_only(self, tmp_path: Path) -> None:
        """Network flags stay on ``memtomem-server`` — and are refused here
        by name rather than silently ignored."""
        result = _stderr_of([str(_MM), "serve", "--transport", "http"], tmp_path)

        assert result.returncode != 0
        assert "--transport" in (result.stderr + result.stdout)

    def test_the_dedicated_entry_point_still_works(self, tmp_path: Path) -> None:
        """``memtomem-server`` is permanent, not deprecated by this command."""
        responses, out = _talk(
            [sys.executable, "-c", "from memtomem.server import main; main([])"],
            tmp_path,
            [_INITIALIZE],
            {1},
        )

        assert 1 in responses, f"stdout={out[:500]}"
        assert "result" in responses[1]
