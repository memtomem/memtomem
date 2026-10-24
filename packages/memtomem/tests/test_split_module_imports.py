"""The modules split out of ``context_cmd`` and ``web/routes/system`` stand on their own.

``context_cmd`` and ``system`` import their split-out modules at the end, to
register commands and routes and to re-export names. A split-out module that
imported its parent at its own top made the parent re-import a half-initialised
module, so importing it first raised ``ImportError``. Inside the suite the
parent is almost always imported already, which hides that, so each import here
runs in a fresh interpreter.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

SPLIT_MODULES = [
    "memtomem.cli.context_sync_cmd",
    "memtomem.cli.context_install_cmd",
    "memtomem.cli.context_bundle_cmd",
    "memtomem.cli.context_settings_cmd",
    "memtomem.web.routes.system_config",
    "memtomem.web.routes.system_indexing",
]


@pytest.mark.parametrize("module", SPLIT_MODULES)
def test_split_module_imports_first_in_a_fresh_interpreter(module: str) -> None:
    # Not ``-I``: isolated mode drops PYTHONPATH, and with it the source tree
    # under test.
    proc = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True,
        text=True,
        timeout=120,
        env=os.environ.copy(),
    )
    assert proc.returncode == 0, proc.stderr


def test_system_routes_keep_a_single_system_tag() -> None:
    from fastapi import FastAPI

    from memtomem.web.routes import system

    app = FastAPI()
    app.include_router(system.router)
    tags = [
        operation.get("tags")
        for path in app.openapi()["paths"].values()
        for operation in path.values()
    ]
    assert tags, "no system routes registered"
    assert all(t == ["system"] for t in tags), sorted({tuple(t or ()) for t in tags})
