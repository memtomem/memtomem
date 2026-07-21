"""Registration invariant: no tool may exist only in ``full`` mode by accident.

``CLAUDE.md`` states that new MCP tools go through the ``@register`` registry
and that only the core 9 are registered directly with ``@mcp.tool()``. Nothing
enforced it, and ``mem_ask`` drifted: it carried ``@mcp.tool()`` without
``@register``, so core/standard pruning (``server/__init__.py``) removed it
while ``mem_do`` — which can only route names present in ``ACTIONS`` — had no
entry to route. The tool existed in ``full`` mode only, and the docs advertised
it unconditionally.

Two halves, because either alone fails open:

* **AST** — a decorator sweep of ``server/tools/*.py`` catches a tool that
  forgets ``@register`` even when the module is imported.
* **Runtime** — comparing the AST sweep against ``ACTIONS`` /
  ``_ALL_REGISTERED_TOOLS`` catches the opposite drift: a module whose
  decorators are correct but which ``server/__init__.py`` never imports, so
  neither the registry nor the tool manager ever sees it.
"""

from __future__ import annotations

import ast
from pathlib import Path

# Importing ``memtomem.server`` is what pulls in every tool module (see the
# import block in ``server/__init__.py``) and therefore what populates both
# ``ACTIONS`` and ``_ALL_REGISTERED_TOOLS`` — ``server.tools`` itself is an
# empty package.
from memtomem.server import _ALL_REGISTERED_TOOLS, _CORE_TOOLS
from memtomem.server.tool_registry import ACTIONS

_TOOLS_DIR = Path(__file__).resolve().parents[1] / "src" / "memtomem" / "server" / "tools"

#: ``mem_context_migrate`` is a deprecated compatibility alias. It is exposed
#: directly in ``full`` mode for old clients, while ``mem_do`` reaches the same
#: implementation through the ``context_migrate`` entry in ``_ALIASES``
#: (``server/tools/meta.py``) — registering it a second time would put a
#: duplicate action in the help catalog. This is the ONLY tool allowed to skip
#: ``@register``; anything else is the ``mem_ask`` bug repeating.
_ALIAS_EXEMPT = frozenset({"mem_context_migrate"})


def _decorated_tools() -> tuple[set[str], set[str]]:
    """Return (``@mcp.tool()`` names, ``@register(...)`` names) found by AST.

    Decorator *spelling* is matched, not identity: a module that imported the
    decorators under another name (``from memtomem.server import mcp as m``)
    would be invisible here. That is exactly why the comparisons below are
    two-way — an AST miss shows up as an unexplained runtime entry.
    """
    direct: set[str] = set()
    registered: set[str] = set()
    for path in sorted(_TOOLS_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            decorators = [ast.unparse(d) for d in node.decorator_list]
            if any(d.startswith("mcp.tool") for d in decorators):
                direct.add(node.name)
            if any(d.startswith("register(") for d in decorators):
                registered.add(node.name)
    return direct, registered


def test_every_non_core_tool_is_registered() -> None:
    """A ``@mcp.tool()`` outside the core 9 must also carry ``@register`` —
    otherwise it vanishes in the default core mode with no ``mem_do`` route."""
    direct, registered = _decorated_tools()
    assert direct, "AST sweep found no @mcp.tool() functions — the sweep itself is broken"
    orphans = direct - set(_CORE_TOOLS) - registered - _ALIAS_EXEMPT
    assert not orphans, (
        f"{sorted(orphans)} are exposed with @mcp.tool() but never @register-ed: "
        "unreachable in core/standard mode and unroutable via mem_do. Add "
        "@register('<category>') (see CLAUDE.md)."
    )


def test_ast_registered_set_matches_runtime_actions() -> None:
    """The ``@register`` sweep and ``ACTIONS`` must agree **exactly**.

    Missing at runtime = a module ``server/__init__.py`` never imports, so the
    decorators are right but the registry is empty. Extra at runtime = the AST
    sweep did not recognise the decorator (aliased import, dynamic
    registration), which would let the first test pass vacuously. Equality is
    what makes both halves fail closed.
    """
    _, registered = _decorated_tools()
    from_ast = {name.removeprefix("mem_") for name in registered}
    assert from_ast == set(ACTIONS), (
        f"registered-but-not-in-ACTIONS: {sorted(from_ast - set(ACTIONS))} "
        "(add the module import to server/__init__.py); "
        f"in-ACTIONS-but-not-found-by-AST: {sorted(set(ACTIONS) - from_ast)} "
        "(the decorator sweep in this file no longer recognises how that tool "
        "registers — fix the sweep, do not delete the assertion)."
    )


def test_ast_direct_set_matches_registered_tool_names() -> None:
    """The ``@mcp.tool()`` sweep and the FastMCP tool manager must agree.

    ``_ALL_REGISTERED_TOOLS`` is the pre-pruning snapshot, so this holds in
    every mode. Equality again, for the same fail-closed reason.
    """
    direct, _ = _decorated_tools()
    assert direct == set(_ALL_REGISTERED_TOOLS), (
        f"decorated-but-unregistered: {sorted(direct - set(_ALL_REGISTERED_TOOLS))} "
        "(add the module import to server/__init__.py); "
        f"registered-but-not-found-by-AST: {sorted(set(_ALL_REGISTERED_TOOLS) - direct)} "
        "(the decorator sweep no longer sees how that tool is exposed — fix "
        "the sweep, do not delete the assertion)."
    )


def test_mem_ask_is_routable_in_core_mode() -> None:
    """Regression pin for the specific drift this module was written for."""
    assert "ask" in ACTIONS, "mem_do(action='ask') must work in the default core mode"
