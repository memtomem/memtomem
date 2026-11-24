"""The modules split out of ``context_cmd`` and ``web/routes/system`` stand on their own.

``context_cmd`` and ``system`` import their split-out modules at the end, to
register commands and routes and to re-export names. A split-out module that
imported its parent at its own top made the parent re-import a half-initialised
module, so importing it first raised ``ImportError``. Inside the suite the
parent is almost always imported already, which hides that, so each import here
runs in a fresh interpreter.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import memtomem
from _subprocess_env import subprocess_env

# Runs in the child after the module import. It exits non-zero, naming both
# directories, unless the child's ``memtomem`` package directory is this
# process's. Both sides resolve ``__init__.py`` first, as ``package_src_dir()``
# does to pick the root it prepends, and ``samefile`` compares the directories
# rather than their spelling.
_SAME_PACKAGE_CHECK = """
import os
import sys
from pathlib import Path

import memtomem

child = Path(memtomem.__file__).resolve().parent
try:
    same = os.path.samefile(child, sys.argv[1])
except OSError as exc:
    sys.exit(f"could not compare {child} with {sys.argv[1]}: {exc}")
if not same:
    sys.exit(f"child imported memtomem from {child}, parent from {sys.argv[1]}")
"""

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
    # Not ``-I``: isolated mode drops PYTHONPATH, which carries the source tree
    # under test. ``-P`` stops ``-c`` from putting the working directory ahead
    # of PYTHONPATH. ``subprocess_env()`` puts the source root of the
    # ``memtomem`` this process imported first on PYTHONPATH.
    parent = Path(memtomem.__file__).resolve().parent
    proc = subprocess.run(
        [sys.executable, "-P", "-c", f"import {module}\n{_SAME_PACKAGE_CHECK}", str(parent)],
        capture_output=True,
        text=True,
        timeout=120,
        env=subprocess_env(),
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
