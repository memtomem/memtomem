"""Importing the package pulls in memtomem's in-process layer only."""

from __future__ import annotations

import subprocess
import sys


def test_import_does_not_load_the_mcp_server_or_the_web_stack() -> None:
    code = (
        "import sys, langchain_memtomem\n"
        "bad = sorted(m for m in sys.modules if m == 'mcp' or m.startswith('mcp.') "
        "or m.startswith('memtomem.server') or m.startswith('memtomem.web'))\n"
        "print(bad)\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], check=True, capture_output=True, text=True, timeout=120
    )
    assert out.stdout.strip() == "[]", out.stdout
