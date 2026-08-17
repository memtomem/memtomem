"""Import-time isolation contract for :mod:`memtomem.runtime`.

In-process embedders (LangGraph / deepagents hosts, notebooks, the quality
harness) build components through ``memtomem.runtime``. That path must not
drag in the MCP server or its SDK: doing so costs import time, pulls a
transport dependency into library use, and re-couples the engine to the
server package the runtime layer was extracted from.

Two complementary checks, because neither alone is enough:

- A **static** sweep over every source file under ``runtime/`` and
  ``integrations/``. It derives its own scope from the tree, so a method the
  runtime checks never call — ``get``, ``delete``, ``log_event``, the scratch
  helpers — cannot introduce a transport import unnoticed. Its detector is
  itself pinned by ``test_detector_*``, in both directions: a form it fails to
  recognise would make the sweep quietly vacuous, and one it over-matches
  would make it useless. ``_transport_imports`` documents the exact syntax it
  recognises; it is a guard against accidental reintroduction, not an
  adversarial sandbox.
- **Runtime** checks that drive the real adapter. They catch the transitive
  case the static sweep misses, but only along the paths they execute.

The runtime checks each run in a subprocess because ``sys.modules`` is
process-global — an earlier test importing ``memtomem.server`` would mask the
regression (the pattern in ``test_langgraph_optional_import.py``).
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

_TRANSPORT_ROOTS = ("mcp", "memtomem.server", "memtomem.web")

# Packages that must stay free of transport imports. Listed as directories, not
# as module names, so the guard walks whatever is there — adding a file to one
# of these puts it under the contract automatically.
_ISOLATED_PACKAGES = ("runtime", "integrations")

_SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "memtomem"


# Loader functions whose literal first argument names a module.
# ``__import__`` is a builtin, so a bare call to it needs no binding; the
# others are only loaders when a binding in this file says so.
_LOADER_FUNCS = frozenset({"import_module", "__import__"})
_LOADER_MODULES = frozenset({"importlib", "builtins"})


def _is_transport(module: str) -> bool:
    return any(module == root or module.startswith(root + ".") for root in _TRANSPORT_ROOTS)


def _package_of(path: Path) -> list[str]:
    """The dotted package a relative import inside *path* is resolved against.

    Dropping the filename gives the containing package for a module
    (``memtomem/integrations/langgraph.py`` → ``memtomem.integrations``) and
    also for a package's ``__init__.py``, where ``.`` means that same package.
    """
    return ["memtomem", *path.relative_to(_SRC_ROOT).parts][:-1]


def _resolve_relative(package: list[str], level: int, module: str | None) -> str | None:
    """Resolve a relative import target to an absolute dotted name.

    Relative imports absolutely can escape their own package — from
    ``integrations/langgraph.py``, ``from ..server import mcp`` is
    ``memtomem.server``. Treating ``level > 0`` as automatically safe was the
    hole this closes.
    """
    keep = len(package) - (level - 1)
    if keep < 0:
        return None  # escapes the source root; not resolvable, not our concern
    return ".".join([*package[:keep], *([module] if module else [])])


def _string_arg(node: ast.Call) -> str | None:
    """The first positional argument of *node*, when it is a string literal."""
    if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
        return node.args[0].value
    return None


def _loader_bindings(tree: ast.AST) -> tuple[set[str], set[str]]:
    """Names this file actually bound to a loader, and to a loader's module.

    Matching a call by the callee's spelling alone cuts both ways: it misses
    ``from importlib import import_module as load`` and it flags
    ``self.import_module(...)`` on an unrelated object. So collect what the
    file really bound:

    - *funcs* — names that call the loader directly: ``import_module``,
      an ``as`` alias of it, or a plain re-binding ``load = import_module``.
      ``__import__`` is always present, being a builtin.
    - *modules* — names bound to ``importlib`` itself, so ``il.import_module``
      counts while ``self.import_module`` does not.

    A name assigned anything else is dropped, so a shadowing parameter or a
    re-binding to an unrelated callable does not leave a stale loader behind.
    Scope is not modelled: a binding anywhere in the file applies to the whole
    file. That is deliberately conservative for a guard whose job is catching
    accidents.
    """
    funcs = {"__import__"}
    modules: set[str] = set()
    rebound: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in _LOADER_MODULES:
            funcs |= {a.asname or a.name for a in node.names if a.name in _LOADER_FUNCS}
        elif isinstance(node, ast.Import):
            modules |= {a.asname or a.name for a in node.names if a.name in _LOADER_MODULES}
        elif isinstance(node, ast.Assign):
            targets = {t.id for t in node.targets if isinstance(t, ast.Name)}
            if isinstance(node.value, ast.Name) and node.value.id in funcs:
                funcs |= targets
            elif isinstance(node.value, ast.Attribute) and node.value.attr in _LOADER_FUNCS:
                funcs |= targets
            else:
                rebound |= targets
        elif isinstance(node, ast.arg):
            rebound.add(node.arg)
    return funcs - rebound, modules - rebound


def _transport_imports(source: str, package: list[str], *, filename: str = "<probe>") -> list[str]:
    """Transport imports in *source*, for a file whose package is *package*.

    Recognised syntax, exhaustively:

    - ``import memtomem.server.x`` and ``from memtomem.server.x import y``
    - ``from memtomem import server`` — the transport is the *member*, not the
      ``ImportFrom`` module
    - ``from ..server import mcp`` — relative, resolved against *package*
    - a call to a loader this file actually bound — ``import_module``, an
      ``as`` alias, a plain re-binding, ``importlib.import_module`` under any
      import alias, or the ``__import__`` builtin — whose first argument is a
      string literal, absolute or leading-dot relative

    Function-scope imports are the ones that bite — invisible at module
    import, so a leak only shows when that branch runs — and ``ast.walk``
    reaches them all, including under ``TYPE_CHECKING`` and ``try/except
    ImportError``.

    Out of scope, deliberately: a module name assembled at runtime (f-string,
    concatenation, variable) and loader machinery below ``import_module``
    (``importlib.machinery``, ``__loader__``). This guard exists to catch a
    transport import reintroduced by accident, which is what regressions
    actually look like; it is not an adversarial sandbox, and pretending
    otherwise would be the more dangerous claim. A transport reached
    indirectly through a helper in another package is also invisible here —
    that is what the lifecycle checks below cover, along the paths they run.
    """
    found: list[str] = []
    tree = ast.parse(source, filename=filename)
    loader_funcs, loader_modules = _loader_bindings(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [a.name for a in node.names if _is_transport(a.name)]
        elif isinstance(node, ast.ImportFrom):
            base = (
                node.module
                if node.level == 0
                else _resolve_relative(package, node.level, node.module)
            )
            if base is None:
                continue
            if _is_transport(base):
                found.append(base)
                continue
            # ``from memtomem import server`` — the transport is the member.
            found += [f"{base}.{a.name}" for a in node.names if _is_transport(f"{base}.{a.name}")]
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute):
                # ``il.import_module(...)`` counts only when ``il`` is bound to
                # importlib; ``self.import_module(...)`` is somebody's method.
                is_loader = (
                    func.attr in _LOADER_FUNCS
                    and isinstance(func.value, ast.Name)
                    and func.value.id in loader_modules
                )
            else:
                is_loader = isinstance(func, ast.Name) and func.id in loader_funcs
            if not is_loader or (arg := _string_arg(node)) is None:
                continue
            if arg.startswith("."):
                level = len(arg) - len(arg.lstrip("."))
                arg = _resolve_relative(package, level, arg.lstrip(".") or None) or ""
            if _is_transport(arg):
                found.append(arg)
    return found


def _transport_imports_in(path: Path) -> list[str]:
    return _transport_imports(
        path.read_text(encoding="utf-8"), _package_of(path), filename=str(path)
    )


def _detect(source: str, *, package: str = "memtomem.integrations") -> list[str]:
    """Run the detector over *source* as if it lived in *package*.

    Parsed in memory on purpose. An earlier version wrote a probe file into
    ``src/memtomem/integrations`` — the very tree the sweep scans — which
    could race with a parallel worker, be observed mid-write by the sweep, or
    survive an abnormal exit.
    """
    return _transport_imports(source, package.split("."))


def test_detector_catches_every_import_form() -> None:
    """Pin the detector itself — an undetected form makes the sweep vacuous.

    Each case is a real way to name a transport module. Without this, the
    sweep passing says only that nothing it *happens* to recognise is present.
    """
    cases = {
        "plain import": "import memtomem.server\n",
        "dotted import": "import memtomem.server.tools.search\n",
        "from-import": "from memtomem.server.tools.search import x\n",
        "member import": "from memtomem import server\n",
        "member import (web)": "from memtomem import web\n",
        "relative parent": "from ..server import mcp\n",
        "relative parent dotted": "from ..server.tools import search\n",
        "mcp sdk": "from mcp.server.mcpserver import MCPServer\n",
        "function scope": "def f():\n    from memtomem.server import mcp\n    return mcp\n",
        "star import": "from memtomem.server import *\n",
        "TYPE_CHECKING only": (
            "from typing import TYPE_CHECKING\n"
            "if TYPE_CHECKING:\n    from memtomem.server import mcp\n"
        ),
        "guarded import": "try:\n    import memtomem.web\nexcept ImportError:\n    pass\n",
        "dynamic import_module": 'from importlib import import_module\nimport_module("memtomem.server")\n',
        "dynamic attribute call": 'import importlib\nimportlib.import_module("memtomem.web")\n',
        "dynamic via module alias": 'import importlib as il\nil.import_module("memtomem.server")\n',
        "dynamic via function alias": (
            'from importlib import import_module as load\nload("memtomem.server")\n'
        ),
        "dynamic via assignment alias": (
            'from importlib import import_module\nload = import_module\nload("memtomem.server")\n'
        ),
        "dynamic via attribute assignment": (
            'import importlib\nload = importlib.import_module\nload("memtomem.server")\n'
        ),
        "dynamic relative literal": (
            'from importlib import import_module\nimport_module("..server", __package__)\n'
        ),
        "dunder import": '__import__("mcp")\n',
    }
    missed = [label for label, src in cases.items() if not _detect(src)]
    assert not missed, f"detector blind to these import forms: {missed}"


def test_detector_does_not_flag_innocent_imports() -> None:
    """The other direction — a detector that flags everything is also useless."""
    cases = {
        "sibling package": "from memtomem.runtime import create_components\n",
        "relative sibling": "from .langgraph_store import MemtomemBaseStore\n",
        "same-package relative": "from . import langgraph\n",
        "stdlib": "import asyncio\n",
        "prefix lookalike": "import memtomem.servers_helper\n",
        "non-literal dynamic": "from importlib import import_module\nimport_module(name)\n",
        "unrelated loader alias": (
            'from importlib import import_module as load\nload("memtomem.runtime")\n'
        ),
        # These carry a *transport* argument on purpose. With a non-transport
        # one they would pass no matter how the callee were classified, which
        # is exactly the vacuous test this replaces.
        "same-name method on another object": 'self.import_module("memtomem.server")\n',
        "attribute on a non-importlib module": (
            'import myloader\nmyloader.import_module("memtomem.server")\n'
        ),
        "shadowing parameter": (
            'def f(import_module):\n    return import_module("memtomem.server")\n'
        ),
        "alias rebound to something else": (
            "from importlib import import_module as load\n"
            "load = other_thing\n"
            'load("memtomem.server")\n'
        ),
        "relative dynamic staying inside": (
            'from importlib import import_module\nimport_module(".langgraph_store", __package__)\n'
        ),
    }
    flagged = {label: hits for label, src in cases.items() if (hits := _detect(src))}
    assert not flagged, f"detector false-positives: {flagged}"


def test_isolated_packages_declare_no_transport_imports() -> None:
    """Static sweep — the guard defines its own scope from the tree."""
    files = sorted(p for pkg in _ISOLATED_PACKAGES for p in (_SRC_ROOT / pkg).rglob("*.py"))
    assert files, f"no sources found under {_ISOLATED_PACKAGES} — guard would vacuously pass"

    offenders = {
        str(p.relative_to(_SRC_ROOT)): mods for p in files if (mods := _transport_imports_in(p))
    }
    assert not offenders, (
        f"transport imports in packages that must stay embeddable in-process: {offenders}"
    )


_ASSERT_CLEAN = f"""
import sys

roots = {_TRANSPORT_ROOTS!r}
leaked = sorted(
    name
    for name in sys.modules
    if any(name == root or name.startswith(root + ".") for root in roots)
)
assert not leaked, "transport modules leaked into the runtime import graph: " + repr(leaked)
"""


def _run(body: str, *, home: Path | None = None) -> None:
    env = None
    if home is not None:
        # Isolate ambient config: MemtomemStore resolves ``config.d`` and
        # persisted overrides from the real home directory otherwise.
        env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home)}
    subprocess.run([sys.executable, "-c", body + _ASSERT_CLEAN], check=True, env=env)


def test_runtime_import_does_not_pull_in_the_mcp_server() -> None:
    _run("import memtomem.runtime\n")


def test_runtime_factory_names_resolve_without_the_mcp_server() -> None:
    _run(
        "from memtomem.runtime import Components, TeardownResult, "
        "create_components, close_components\n"
    )


def test_components_can_be_built_and_closed_without_the_mcp_server(tmp_path) -> None:
    """The full build/teardown cycle, not just the import.

    A lazy ``import memtomem.server`` inside ``create_components`` would pass
    the import-only checks above, so exercise the real path against an
    isolated on-disk store with embeddings disabled.
    """
    db_path = tmp_path / "hygiene.db"
    _run(
        f"""
import asyncio

from memtomem.config import Mem2MemConfig
from memtomem.runtime import close_components, create_components

config = Mem2MemConfig()
config.storage.sqlite_path = {str(db_path)!r}
config.embedding.provider = "none"
config.embedding.dimension = 0

async def main():
    comp = await create_components(config, load_ambient_config=False)
    try:
        assert comp.search_pipeline is not None
        assert comp.index_engine is not None
    finally:
        await close_components(comp)

asyncio.run(main())
"""
    )


def test_memtomem_store_full_session_cycle_stays_off_the_transport_stack(tmp_path) -> None:
    """The named consumer, not just the layer it is supposed to use.

    The checks above import ``memtomem.runtime`` directly, so they would stay
    green if ``MemtomemStore`` quietly went back to
    ``memtomem.server.component_factory`` — the regression this whole move
    exists to prevent. Drive the adapter itself instead.

    What runs here is an agent's lifetime: the multi-agent session bracket, a
    write, a read, teardown. That is *not* the whole adapter surface —
    ``get``, ``delete``, ``index``, ``log_event``, and the scratch helpers are
    never executed. Declared transport imports on those paths are caught
    statically by
    :func:`test_isolated_packages_declare_no_transport_imports`; what this
    check adds is the transitive case, where a helper elsewhere pulls a
    transport in behind an import the static sweep sees as innocent.
    """
    home = tmp_path / "home"
    home.mkdir()
    memory_dir = tmp_path / "memories"
    memory_dir.mkdir()
    db_path = tmp_path / "store.db"
    _run(
        f"""
import asyncio

from memtomem.integrations import MemtomemStore  # lazy package export

store = MemtomemStore(
    config_overrides={{
        "storage": {{"sqlite_path": {str(db_path)!r}}},
        "embedding": {{"provider": "none", "dimension": 0}},
        "indexing": {{"memory_dirs": [{str(memory_dir)!r}]}},
    }}
)

async def main():
    comp = await store._ensure_init()
    assert comp.search_pipeline is not None
    await store.start_agent_session("planner")
    # add/search resolve the ADR-0011 project-context anchor; end_session with
    # a summary stamps SUMMARY_PROVENANCE_MANUAL, the constant this change
    # relocated out of server/tools/_provenance.py.
    await store.add("cache strategy is write-through", tags=["arch"])
    hits = await store.search("cache strategy")
    assert isinstance(hits, list)
    ended = await store.end_session(summary="decided on write-through")
    assert isinstance(ended, dict)
    await store.close()
    assert store._components is None

asyncio.run(main())
""",
        home=home,
    )
