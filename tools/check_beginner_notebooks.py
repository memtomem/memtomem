"""Execute the two no-model notebooks in fresh kernels without modifying them.

Run with the minimal environment's Python. Outputs stay in memory. No model
or API SDK is required. Network attempts from notebook Python are rejected.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

import nbformat
from nbclient import NotebookClient

ROOT = Path(__file__).resolve().parents[1]
NAMES = ("05_langgraph_memory_basics.ipynb", "06_langgraph_retrieval_memory.ipynb")


def execute_notebook(name: str) -> dict:
    notebook = nbformat.read(ROOT / "examples" / "notebooks" / name, as_version=4)
    guard = nbformat.v4.new_code_cell(
        "import socket\n"
        "def _deny_network(*args, **kwargs):\n"
        "    raise RuntimeError('Offline notebook attempted network access')\n"
        "socket.socket.connect = _deny_network\n"
        "socket.create_connection = _deny_network\n"
    )
    notebook.cells.insert(0, guard)
    with tempfile.TemporaryDirectory(prefix="memtomem-nb-check-") as temporary:
        allowed = {"PATH", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT", "WINDIR"}
        env = {key: value for key, value in os.environ.items() if key in allowed}
        env.update(
            HOME=temporary,
            USERPROFILE=temporary,
            XDG_CONFIG_HOME=temporary,
            XDG_DATA_HOME=temporary,
            XDG_CACHE_HOME=temporary,
            XDG_STATE_HOME=temporary,
            IPYTHONDIR=temporary,
            JUPYTER_CONFIG_DIR=temporary,
            LANGSMITH_TRACING="false",
            LANGCHAIN_TRACING_V2="false",
        )
        client = NotebookClient(notebook, timeout=120, kernel_name="python3")
        manager = client.create_kernel_manager()
        manager.kernel_spec.argv = [
            sys.executable,
            "-m",
            "ipykernel_launcher",
            "-f",
            "{connection_file}",
        ]
        client.km = manager
        client.execute(cwd=temporary, env=env, cleanup_kc=True)
    text = "".join(
        output.get("text", "")
        for cell in notebook.cells
        for output in cell.get("outputs", [])
        if output.output_type == "stream"
    )
    assert text.count("PASS ") == 6, text
    if name.startswith("06"):
        assert "SKIP LLM" in text, text
    return {"notebook": name, "checks": 6, "status": "PASS", "network": "blocked"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    results = [execute_notebook(name) for name in NAMES]
    if args.json:
        print(json.dumps(results, indent=2))
    else:
        for result in results:
            print(f"{result['status']} {result['notebook']} ({result['checks']} checks)")


if __name__ == "__main__":
    main()
