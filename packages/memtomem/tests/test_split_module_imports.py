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
# process's. Neither side resolves ``__init__.py``: one that links into another
# checkout would make that checkout's directory look like this one, though its
# submodules differ (#3574). ``samefile`` compares the directories rather than
# their spelling, so a linked package directory still counts as itself.
_SAME_PACKAGE_CHECK = """
import os
import sys
from pathlib import Path

import memtomem

child = Path(memtomem.__file__).parent
try:
    same = os.path.samefile(child, sys.argv[1])
except OSError as exc:
    sys.exit(f"could not compare {child} with {sys.argv[1]}: {exc}")
if not same:
    sys.exit(f"child imported memtomem from {child}, parent from {sys.argv[1]}")
"""


def _parent_package_dir() -> Path:
    # The parent's side of the check, kept apart from ``package_src_dir()`` so a
    # wrong root there cannot also pass here. Not resolved, for the same reason
    # as the child side.
    return Path(memtomem.__file__).parent


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
    parent = _parent_package_dir()
    proc = subprocess.run(
        [sys.executable, "-P", "-c", f"import {module}\n{_SAME_PACKAGE_CHECK}", str(parent)],
        capture_output=True,
        text=True,
        timeout=120,
        env=subprocess_env(),
    )
    assert proc.returncode == 0, proc.stderr


@pytest.mark.requires_symlinks
@pytest.mark.parametrize("own_tree", [False, True], ids=["linked-tree", "own-tree"])
def test_same_package_check_tells_a_linked_initializer_apart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, own_tree: bool
) -> None:
    # ``b/memtomem/__init__.py`` links into ``a``, so following it makes a child
    # that imported ``b`` look like ``a`` (#3574). The child imports a stand-in
    # package from ``b``; ``-I -S`` keeps inherited PYTHONPATH, ``.pth`` files and
    # ``sitecustomize`` from supplying another ``memtomem``. In the own-tree case
    # the parent side comes from ``_parent_package_dir()`` with this process's
    # ``memtomem`` placed in ``b`` too, so both sides of the check are exercised.
    (tmp_path / "a" / "memtomem").mkdir(parents=True)
    (tmp_path / "a" / "memtomem" / "__init__.py").write_text("")
    (tmp_path / "b" / "memtomem").mkdir(parents=True)
    try:
        (tmp_path / "b" / "memtomem" / "__init__.py").symlink_to(
            tmp_path / "a" / "memtomem" / "__init__.py"
        )
    except (OSError, NotImplementedError):
        # The marker probes the system temp directory, not ``--basetemp``.
        pytest.skip("symlink creation is not permitted under tmp_path")
    if own_tree:
        monkeypatch.setattr(memtomem, "__file__", str(tmp_path / "b" / "memtomem" / "__init__.py"))
        parent = _parent_package_dir()
    else:
        parent = tmp_path / "a" / "memtomem"
    code = "import sys\nsys.path.insert(0, sys.argv.pop(2))\n" + _SAME_PACKAGE_CHECK
    proc = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            code,
            str(parent),
            str(tmp_path / "b"),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if own_tree:
        assert proc.returncode == 0, proc.stderr
    else:
        assert proc.returncode != 0
        assert "child imported memtomem from" in proc.stderr, proc.stderr


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
