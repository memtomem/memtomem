"""Execute the no-model notebooks in fresh kernels without modifying them.

Run with the minimal environment's Python. Outputs stay in memory unless --html-dir is set. No model
or API SDK is required. The socket APIs listed in ``_BLOCKED_SOCKET_APIS``
are rejected inside the kernel; that is the guard's exact reach.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time

import nbformat
from nbclient import NotebookClient

ROOT = Path(__file__).resolve().parents[1]
NAMES = (
    "00_start_here.ipynb",
    "05_langgraph_memory_basics.ipynb",
    "06_langgraph_retrieval_memory.ipynb",
)
# The outbound calls a notebook could make. This is the guard's exact reach:
# it does not cover a reference bound before the guard cell ran, nor the
# lower-level ``_socket`` module.
_BLOCKED_SOCKET_APIS = (
    "socket.socket.connect",
    "socket.socket.connect_ex",
    "socket.socket.sendto",
    "socket.create_connection",
)


def execute_notebook(name: str, repeat: int = 1, html_dir: Path | None = None) -> dict:
    started = time.monotonic()
    notebook = nbformat.read(ROOT / "examples" / "notebooks" / name, as_version=4)
    original_cells = notebook.cells
    notebook.cells = []
    for iteration in range(repeat):
        for original in original_cells:
            cell = copy.deepcopy(original)
            cell.id = f"{cell.id}-{iteration}"
            notebook.cells.append(cell)
    guard = nbformat.v4.new_code_cell(
        "import socket\n"
        "def _deny_network(*args, **kwargs):\n"
        "    raise RuntimeError('Offline notebook attempted network access')\n"
        + "".join(f"{target} = _deny_network\n" for target in _BLOCKED_SOCKET_APIS)
    )
    notebook.cells.insert(0, guard)
    with tempfile.TemporaryDirectory(prefix="memtomem-nb-check-") as temporary:
        if name == "00_start_here.ipynb":
            shutil.copytree(
                ROOT / "examples/onboarding/slateharbor",
                Path(temporary) / "examples/onboarding/slateharbor",
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
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
        client = NotebookClient(notebook, timeout=180, kernel_name="python3")
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
    if name == "00_start_here.ipynb":
        assert text.count("SLATEHARBOR COMPLETE") == repeat, text
    else:
        assert text.count("PASS ") == 6 * repeat, text
    if name.startswith("06"):
        assert "SKIP LLM" in text, text
    if html_dir is not None:
        from nbconvert import HTMLExporter

        # Remove only the injected network guard from the presentation artifact.
        exported = copy.deepcopy(notebook)
        exported.cells = exported.cells[1:]
        body, _ = HTMLExporter().from_notebook_node(exported)
        html_dir.mkdir(parents=True, exist_ok=True)
        (html_dir / f"{Path(name).stem}.html").write_text(body, encoding="utf-8")
    return {
        "notebook": name,
        "checks": "Run All assertions" if name.startswith("00") else 6,
        "status": "PASS",
        "network_blocked": list(_BLOCKED_SOCKET_APIS),
        # The guard on the CLI children 00 spawns lives in lab.py's bootstrap and
        # is probed per API by slateharbor/validate.py. This runner does not
        # observe it, so it does not report on it.
        "same_kernel_runs": repeat,
        "elapsed_seconds": round(time.monotonic() - started, 2),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--notebook", choices=NAMES, help="Execute one notebook instead of all")
    parser.add_argument(
        "--repeat", type=int, choices=(1, 2), default=1, help="Repeat Run All in the same kernel"
    )
    parser.add_argument(
        "--html-dir", type=Path, help="Export executed HTML previews (requires nbconvert)"
    )
    args = parser.parse_args()
    results = [
        execute_notebook(name, args.repeat, args.html_dir)
        for name in ((args.notebook,) if args.notebook else NAMES)
    ]
    if args.json:
        print(json.dumps(results, indent=2))
    else:
        for result in results:
            print(f"{result['status']} {result['notebook']} ({result['checks']} checks)")


if __name__ == "__main__":
    main()
