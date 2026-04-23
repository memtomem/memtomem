"""Regression guard: every @mcp.tool()-decorated function must be importable.

If a new tool module is added with @mcp.tool() but never imported in
server/__init__.py, the decorator never fires and the tool is invisible
at runtime.  This test catches that by comparing AST-parsed source
definitions against the actual import statements in __init__.py.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src" / "memtomem" / "server"
_TOOLS_DIR = _SRC / "tools"
_INIT_FILE = _SRC / "__init__.py"


def _modules_with_mcp_tool() -> dict[str, set[str]]:
    """Return {module_stem: {func_names}} for every tools/*.py with @mcp.tool()."""
    result: dict[str, set[str]] = {}
    for py in _TOOLS_DIR.glob("*.py"):
        if py.name == "__init__.py":
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"))
        names: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            for dec in node.decorator_list:
                src = ast.dump(dec)
                if "mcp" in src and "tool" in src:
                    names.add(node.name)
        if names:
            result[py.stem] = names
    return result


def _imported_tool_modules() -> set[str]:
    """Return module stems imported from ``memtomem.server.tools.*`` in __init__.py."""
    tree = ast.parse(_INIT_FILE.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module
            and node.module.startswith("memtomem.server.tools.")
        ):
            modules.add(node.module.rsplit(".", 1)[-1])
    return modules


def test_all_mcp_tool_modules_imported():
    """Every tools/*.py that defines @mcp.tool() must be imported in __init__.py.

    When a module is imported, all its @mcp.tool() decorators fire and
    register the tools with the FastMCP instance.  A missing import means
    the tool silently disappears from the server.
    """
    defined = _modules_with_mcp_tool()
    imported = _imported_tool_modules()
    missing = set(defined.keys()) - imported

    if missing:
        lines = [f"  {mod}: {', '.join(sorted(defined[mod]))}" for mod in sorted(missing)]
        msg = "Tool modules with @mcp.tool() not imported in server/__init__.py:\n" + "\n".join(
            lines
        )
        raise AssertionError(msg)


def _files_importing_bare_get_app() -> list[Path]:
    """Return handler files that import ``_get_app`` instead of ``_get_app_initialized``.

    Issue #399 requires every MCP tool / resource handler to fetch the
    app via ``_get_app_initialized`` so the first handler call (not the
    lifespan handshake) triggers DB + embedder creation. A file still
    importing bare ``_get_app`` from ``memtomem.server.context`` is a
    regression — Phase 3 will drop the eager ``ensure_initialized`` call
    in ``app_lifespan`` and such a handler would then operate on an
    uninitialised context.
    """
    offenders: list[Path] = []
    candidates: list[Path] = [_SRC / "resources.py", *_TOOLS_DIR.glob("*.py")]
    for py in candidates:
        if py.name == "__init__.py":
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.module != "memtomem.server.context":
                continue
            for alias in node.names:
                if alias.name == "_get_app":
                    offenders.append(py)
                    break
    return offenders


def test_handlers_use_get_app_initialized():
    """Tool / resource modules must import ``_get_app_initialized``, not ``_get_app``.

    Guards the Phase 2 handler migration (issue #399): every handler has
    to await ``ensure_initialized`` before touching storage / embedder /
    index_engine / search_pipeline. Using ``_get_app`` bypasses that step
    and will crash on a read once Phase 3 removes the lifespan's eager
    init.
    """
    offenders = _files_importing_bare_get_app()
    if offenders:
        rel = sorted(str(p.relative_to(_SRC.parent)) for p in offenders)
        msg = (
            "Handler files importing bare ``_get_app`` — migrate to "
            "``_get_app_initialized`` (issue #399 Phase 2):\n" + "\n".join(f"  {p}" for p in rel)
        )
        raise AssertionError(msg)
