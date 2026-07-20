"""Every MCP tool parameter must be documented, and the safety valve twice.

Two surfaces read these docstrings: the wire ``description`` a client gets for
an exposed tool, and ``mem_do(action="help", params={"category": ...})`` — the
only parameter documentation that exists for a non-core action in the default
core mode. Fourteen tools had parameters but no ``Args:`` block at all, so the
help catalog listed bare types (``scope: TargetScope = 'user'``) and an agent
had to guess. Four of them hid ``force_unsafe``, the redaction-guard bypass.

The coverage check is deliberately scoped to the whole tool surface rather
than to ``ACTIONS``: a direct-only tool (``mem_context_migrate``) and the
register-only actions (``mem_ingest``, ``mem_increment_access``,
``mem_version``) each appear on exactly one of the two surfaces, and both are
read by somebody.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_TOOLS_DIR = Path(__file__).resolve().parents[1] / "src" / "memtomem" / "server" / "tools"


def _tool_functions() -> list[tuple[str, ast.AsyncFunctionDef | ast.FunctionDef]]:
    """Every decorated tool function in ``server/tools`` (public names only)."""
    found: list[tuple[str, ast.AsyncFunctionDef | ast.FunctionDef]] = []
    for path in sorted(_TOOLS_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            decorators = [ast.unparse(d) for d in node.decorator_list]
            exposed = any(d.startswith("mcp.tool") for d in decorators)
            routed = any(d.startswith("register(") for d in decorators)
            if exposed or routed:
                found.append((path.name, node))
    return found


def _params(node: ast.AsyncFunctionDef | ast.FunctionDef) -> list[str]:
    return [a.arg for a in node.args.args + node.args.kwonlyargs if a.arg != "ctx"]


def _documented(node: ast.AsyncFunctionDef | ast.FunctionDef) -> set[str]:
    """Parameter names that appear as ``name:`` entries in the docstring.

    Matched textually rather than through ``_parse_arg_docs`` on purpose: this
    guard asks whether a human wrote the documentation, and should keep failing
    for an undocumented parameter even if the parser changes shape.
    """
    doc = ast.get_docstring(node) or ""
    return {
        line.strip().split(":", 1)[0]
        for line in doc.splitlines()
        if ":" in line and line.startswith(" ")
    }


def test_sweep_finds_the_tool_surface() -> None:
    """Guard the guard: an empty sweep would make every check below vacuous."""
    assert len(_tool_functions()) > 90


def test_every_tool_parameter_is_documented() -> None:
    offenders = {
        f"{filename}::{node.name}": missing
        for filename, node in _tool_functions()
        if (missing := sorted(set(_params(node)) - _documented(node)))
    }
    assert not offenders, (
        f"tool parameters with no Args: entry: {offenders}. In core mode "
        "mem_do(action='help') shows only the type for these, so an agent has "
        "to guess what to pass."
    )


#: Each entry is (alternative spellings, what the clause has to convey).
#: Alternatives exist because the existing docs say "redaction gate" in one
#: place and "redaction guard" in another; both state the same contract, and
#: forcing one spelling would be churn, not clarity.
_SAFETY_MARKERS = (
    (("redaction guard", "redaction gate"), "what the valve actually bypasses"),
    (("bypassed", "audit-log", "audit line"), "that the bypass is recorded, not silent"),
)

#: Tools whose write path cannot reach ``project_shared``, so the "never
#: bypassable for project_shared" clause would be a false statement in their
#: docs. ``mem_import`` calls ``enforce_write_guard(..., scope="user")``
#: unconditionally (``memtomem/tools/export_import.py``), so the scope-aware
#: refusal never fires for it. Everything else must carry the clause.
_NO_PROJECT_SHARED_PATH = {
    "mem_import": "guard is invoked with scope='user' in tools/export_import.py",
}


def _force_unsafe_tools() -> list[tuple[str, ast.AsyncFunctionDef | ast.FunctionDef]]:
    """Derived from signatures, so a sixth tool cannot slip in undocumented."""
    return [(f, n) for f, n in _tool_functions() if "force_unsafe" in _params(n)]


def test_force_unsafe_tools_are_found() -> None:
    assert len(_force_unsafe_tools()) >= 5


@pytest.mark.parametrize(
    "markers,why", _SAFETY_MARKERS, ids=[m[0][0].replace(" ", "-") for m in _SAFETY_MARKERS]
)
def test_force_unsafe_documentation_states_the_contract(markers: tuple[str, ...], why: str) -> None:
    """Non-emptiness is not enough for the redaction bypass.

    ``force_unsafe`` is the one parameter whose misuse writes a secret to
    disk, so its description has to carry the contract itself — an agent
    reading the help catalog has nothing else to consult.
    """
    offenders = [
        f"{filename}::{node.name}"
        for filename, node in _force_unsafe_tools()
        if not any(marker in (ast.get_docstring(node) or "") for marker in markers)
    ]
    assert not offenders, f"force_unsafe docs must state {why}: missing in {offenders}"


def test_force_unsafe_documentation_states_the_project_shared_refusal() -> None:
    """Scope-aware: only claim it where the refusal can actually fire.

    ``enforce_write_guard`` hard-refuses a bypass when ``scope`` is
    ``project_shared``, so every tool that can write there must say so. A
    tool that always passes ``scope="user"`` must NOT say so — a false safety
    claim is worse than a missing one — hence the justified exemption list.
    """
    offenders = [
        f"{filename}::{node.name}"
        for filename, node in _force_unsafe_tools()
        if node.name not in _NO_PROJECT_SHARED_PATH
        and "project_shared" not in (ast.get_docstring(node) or "")
    ]
    assert not offenders, (
        "force_unsafe docs must state that project_shared is never bypassable: "
        f"missing in {offenders}"
    )


def test_project_shared_exemptions_are_still_exempt() -> None:
    """The exemption list must not outlive its justification.

    If ``mem_import`` ever starts passing a real scope, the clause becomes
    required and this test says so instead of silently keeping the tool out
    of the check.
    """
    source = (
        Path(__file__).resolve().parents[1] / "src" / "memtomem" / "tools" / "export_import.py"
    ).read_text(encoding="utf-8")
    assert 'scope="user"' in source, (
        "tools/export_import.py no longer pins scope='user' — mem_import can now "
        "reach a scope-aware refusal, so drop it from _NO_PROJECT_SHARED_PATH "
        "and document the project_shared clause on it"
    )
