"""Prevent new partial configuration stacks outside canonical loading (#2399).

The existing callers below are a bounded migration backlog, not approved
examples to copy. Function names and counts are pinned so deleting a caller
also requires deleting its exemption; whole modules are never exempted.
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "src" / "memtomem"
LOADERS = {"load_config_d", "load_config_overrides"}

# Counts are (fragment-loader references, override-loader references). This helper owns the
# complete canonical sequence, including final profile normalization.
CANONICAL = {"config_signature.py:_build_config": (1, 1)}
DEFERRED = {
    # CLI lifecycle and diagnostic contracts need their own migration review.
    "cli/context_cmd.py:_resolve_cli_scope": (1, 1),
    "cli/embedding_cmd.py:_run": (1, 1),
    "cli/mem_cmd.py:rescan_files_cmd": (1, 1),
    "cli/memory_doctor_cmd.py:_load_config_read_only": (1, 1),
    "cli/reset_cmd.py:_run": (1, 1),
    "cli/sync_doctor_cmd.py:sync_doctor": (1, 0),
    "cli/uninstall_cmd.py:_load_config_safely": (1, 1),
    "cli/upgrade_cmd.py:_resolve_db_path": (1, 1),
    # Integration constructors also accept explicit caller-supplied configs.
    "integrations/langgraph.py:MemtomemStore._ensure_init": (1, 1),
    "integrations/langgraph_hybrid_store.py:MemtomemHybridStore.__init__": (1, 1),
    "integrations/langgraph_store.py:MemtomemBaseStore.__init__": (1, 1),
    "integrations/langgraph_store.py:MemtomemBaseStore.__init__._registered_project_dirs": (1, 1),
    # Startup, tracing and MCP failure/recovery policies remain unchanged here.
    "observability/session_tracing.py:get_trace_config": (1, 1),
    "runtime/components.py:create_components": (1, 1),
    "server/__init__.py:_resolve_store_db_path": (1, 1),
    "server/tools/context.py:_resolve_mcp_scope": (1, 1),
    "server/tools/status_config.py:collect_runtime_profile": (1, 1),
}


def loader_references(source: str) -> dict[str, tuple[int, int]]:
    """Count loader references, including escape through an alias binding.

    Catch ``loader = load_config_d`` at the binding, rather than trying to
    trace later calls or emulate Python's scope and assignment rules. Imported
    spellings are matched conservatively regardless of module (including
    relative imports and re-exports). Comments and import declarations alone
    are not counted; a reference in executable code is required.
    """
    tree = ast.parse(source)
    aliases = {name: name for name in LOADERS}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for item in node.names:
                if item.name in LOADERS:
                    aliases[item.asname or item.name] = item.name

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.scope: list[str] = []
            self.references: dict[str, Counter[str]] = {}

        def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()

        visit_AsyncFunctionDef = visit_FunctionDef
        visit_ClassDef = visit_FunctionDef

        def record(self, name: str) -> None:
            if name in LOADERS:
                owner = ".".join(self.scope) or "<module>"
                self.references.setdefault(owner, Counter())[name] += 1

        def visit_Name(self, node: ast.Name):
            if isinstance(node.ctx, ast.Load):
                self.record(aliases.get(node.id, ""))

        def visit_Attribute(self, node: ast.Attribute):
            if isinstance(node.ctx, ast.Load):
                self.record(node.attr)
            self.generic_visit(node)

    visitor = Visitor()
    visitor.visit(tree)
    return {
        owner: (references["load_config_d"], references["load_config_overrides"])
        for owner, references in visitor.references.items()
    }


def test_no_new_manual_config_loaders() -> None:
    actual = {
        f"{path.relative_to(ROOT).as_posix()}:{owner}": counts
        for path in sorted(ROOT.rglob("*.py"))
        for owner, counts in loader_references(path.read_text(encoding="utf-8")).items()
    }
    assert actual == CANONICAL | DEFERRED, (
        "Use build_fresh_config/build_comparand for new loads; remove stale exemptions. "
        f"Unexpected or changed: {actual.items() - (CANONICAL | DEFERRED).items()}; "
        f"missing: {(CANONICAL | DEFERRED).items() - actual.items()}"
    )


@pytest.mark.parametrize(
    "call",
    ["load_config_d(cfg)", "fragments(cfg)", "config.load_config_d(cfg)"],
)
def test_guard_detects_new_loader_spellings(call: str) -> None:
    source = (
        "from memtomem.config import load_config_d as fragments\n"
        "def outer():\n"
        "    def inner():\n"
        f"        {call}\n"
    )
    assert loader_references(source) == {"outer.inner": (1, 0)}


def test_guard_counts_repeated_loads_and_ignores_comments() -> None:
    assert loader_references(
        "# load_config_d(cfg)\ndef reader():\n"
        "    load_config_overrides(cfg)\n    load_config_overrides(cfg)\n"
    ) == {"reader": (0, 2)}


@pytest.mark.parametrize(
    "binding",
    [
        "loader = load_config_d",
        "first = fragments; loader = first",
        "loader: object = config.load_config_d",
        "first = loader = load_config_d",
    ],
)
def test_guard_detects_local_loader_aliases(binding: str) -> None:
    assert loader_references(
        "from memtomem.config import load_config_d as fragments\n"
        f"def reader():\n    {binding}\n    loader(cfg)\n"
    ) == {"reader": (1, 0)}


def test_guard_catches_binding_without_tracing_its_uses() -> None:
    assert loader_references(
        "def first():\n    loader = load_config_d\n    loader(cfg)\n"
        "    loader = unrelated\n    loader(cfg)\n"
        "def second():\n    loader(cfg)\n"
    ) == {"first": (1, 0)}


@pytest.mark.parametrize("module", ["memtomem.config", "memtomem.config_signature", ".config"])
@pytest.mark.parametrize("alias", ["load_config_d", "fragment_loader"])
def test_guard_covers_relative_and_reexported_imports(module: str, alias: str) -> None:
    assert loader_references(
        f"from {module} import load_config_d as {alias}\ndef reader():\n    {alias}(cfg)\n"
    ) == {"reader": (1, 0)}


def test_guard_catches_escaping_loader_even_without_a_local_call() -> None:
    assert loader_references("def reader():\n    return load_config_overrides\n") == {
        "reader": (0, 1)
    }
