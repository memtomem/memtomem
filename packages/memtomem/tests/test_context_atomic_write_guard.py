"""Architectural guard for the context-gateway atomic-write invariant.

``context/_atomic.py`` states: "Every gateway write site funnels through the
helpers in this module" — a crash between truncate and flush of a bare
``Path.write_text`` leaves the target empty or half-written. The artifact
fan-out families (agents, commands, skills, settings, MCP servers, web
mutators) were converted in #283, but the invariant was docstring-only: the
project-memory fan-out and the canonical ``context.md`` writes shipped six
bare ``write_text`` sites that survived until the #1247 triage (id 19).

This file makes the contract enforced rather than aspirational (an
unenforced policy is no policy): AST-scan every gateway write surface for
bare ``.write_text(`` / ``.write_bytes(`` calls. A future write site must
either use ``atomic_write_text`` / ``atomic_write_bytes`` or be added to
:data:`ALLOWED_BARE_WRITES` with an inline rationale — a silent gap stops
being representable.

Scope note: only production gateway write surfaces are scanned. Tests freely
use bare writes to SEED fixtures — that is reading-side setup, not the
crash-safety surface this guards.
"""

from __future__ import annotations

import ast
import pathlib

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "memtomem"

# Methods whose bare use on a Path-like target re-opens the truncate/flush
# crash window the atomic helpers exist to close.
_BARE_WRITE_ATTRS = frozenset({"write_text", "write_bytes"})

# ``(path relative to src/memtomem, line_function)`` pairs explicitly allowed
# to use a bare write. Empty as of #1247 B4 — every gateway write site goes
# through the helpers. Add an entry ONLY with an inline why (e.g. a target
# whose partial loss is provably harmless), mirroring the DEFERRED registry
# convention in test_validate_namespace_architectural_guard.py.
ALLOWED_BARE_WRITES: frozenset[tuple[str, str]] = frozenset()


def _gateway_write_files() -> list[pathlib.Path]:
    """Every production module that writes context-gateway artifacts.

    * ``context/*.py`` — the engine + per-artifact fan-out/import modules.
    * ``cli/context_cmd.py`` / ``server/tools/context.py`` — the CLI and MCP
      surfaces carrying the inline project-memory fan-out (#1247 id 19).
    * ``web/routes/context_*.py`` — the web mutators.
    """
    files = sorted((_SRC / "context").glob("*.py"))
    files.append(_SRC / "cli" / "context_cmd.py")
    files.append(_SRC / "server" / "tools" / "context.py")
    files.extend(sorted((_SRC / "web" / "routes").glob("context_*.py")))
    return files


def _enclosing_function(tree: ast.AST, lineno: int) -> str:
    """Best-effort name of the function containing *lineno* (for the report)."""
    best = "<module>"
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = getattr(node, "end_lineno", node.lineno)
            if node.lineno <= lineno <= end:
                best = node.name
    return best


def test_gateway_files_exist() -> None:
    """The scan list must not silently go stale on a rename/move."""
    for f in _gateway_write_files():
        assert f.is_file(), f"guard scan list references a missing file: {f}"


def test_no_bare_writes_on_gateway_surfaces() -> None:
    # Flag ATTRIBUTE REFERENCES, not just direct calls: three of the six
    # #1247 id-19 sites were ``asyncio.to_thread(out_path.write_text, ...)``,
    # where ``.write_text`` is an uncalled attribute handed to the executor —
    # a Call-only scan walks right past the exact shape being guarded.
    offenders: list[str] = []
    for f in _gateway_write_files():
        tree = ast.parse(f.read_text(encoding="utf-8"))
        rel = str(f.relative_to(_SRC))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in _BARE_WRITE_ATTRS:
                fn = _enclosing_function(tree, node.lineno)
                if (rel, fn) in ALLOWED_BARE_WRITES:
                    continue
                offenders.append(f"{rel}:{node.lineno} ({fn}) .{node.attr}")
    assert not offenders, (
        "bare write on a gateway surface — use context._atomic.atomic_write_text/"
        "atomic_write_bytes (crash mid-write must not truncate the target), or add "
        "an ALLOWED_BARE_WRITES entry with a rationale:\n  " + "\n  ".join(offenders)
    )
