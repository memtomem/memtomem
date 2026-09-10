"""Validate example notebooks: JSON structure + Python syntax of code cells.

These checks run without Ollama or any external service and catch broken
notebook JSON (merge conflicts, bad edits) and syntax errors in code cells
after refactors.
"""

from __future__ import annotations

import ast
import json
import textwrap
from pathlib import Path

import pytest

NOTEBOOKS_DIR = Path(__file__).resolve().parents[3] / "examples" / "notebooks"
EXPECTED_NOTEBOOKS = (
    "00_start_here.ipynb",
    "01_hello_memory.ipynb",
    "02_index_and_filter.ipynb",
    "03_agent_memory_patterns.ipynb",
    "04_multi_agent_mcp_memory.ipynb",
    "05_langgraph_memory_basics.ipynb",
    "06_langgraph_retrieval_memory.ipynb",
)

# The model-free beginner labs. Named rather than sliced off EXPECTED_NOTEBOOKS
# so that adding a 07 cannot silently retarget the checks below.
BEGINNER_NOTEBOOKS = (
    "05_langgraph_memory_basics.ipynb",
    "06_langgraph_retrieval_memory.ipynb",
)
OPTIONAL_LLM_NOTEBOOK = "06_langgraph_retrieval_memory.ipynb"


def _compile_cell(source: str) -> None:
    """Compile a code cell, retrying inside ``async def`` for top-level await."""
    try:
        ast.parse(source)
    except SyntaxError:
        # Notebooks support top-level await; wrap and retry.
        wrapped = "async def __nb_cell__():\n" + textwrap.indent(source, "    ")
        ast.parse(wrapped)


def _notebook_files() -> list[Path]:
    if not NOTEBOOKS_DIR.is_dir():
        return []
    return sorted(NOTEBOOKS_DIR.glob("*.ipynb"))


def test_notebook_inventory() -> None:
    """An absent/empty notebook directory must not false-green parametrization."""
    assert tuple(path.name for path in _notebook_files()) == EXPECTED_NOTEBOOKS


@pytest.mark.parametrize("notebook", _notebook_files(), ids=lambda p: p.stem)
def test_notebook_structure(notebook: Path) -> None:
    """Notebook must be valid JSON with the expected top-level keys."""
    data = json.loads(notebook.read_text(encoding="utf-8"))
    assert "cells" in data, "missing 'cells'"
    assert "metadata" in data, "missing 'metadata'"
    assert "nbformat" in data, "missing 'nbformat'"


@pytest.mark.parametrize("notebook", _notebook_files(), ids=lambda p: p.stem)
def test_notebook_code_syntax(notebook: Path) -> None:
    """Every code cell must have valid Python syntax."""
    data = json.loads(notebook.read_text(encoding="utf-8"))
    for idx, cell in enumerate(data["cells"]):
        if cell["cell_type"] != "code":
            continue
        source = "".join(cell["source"])
        if not source.strip():
            continue
        try:
            _compile_cell(source)
        except SyntaxError as exc:
            pytest.fail(f"Cell {idx} in {notebook.name}: {exc}")


_IMPORT_EXCEPTIONS = frozenset({"ModuleNotFoundError", "ImportError"})


@pytest.mark.parametrize("name", BEGINNER_NOTEBOOKS)
def test_beginner_notebooks_ship_without_outputs(name: str) -> None:
    data = json.loads((NOTEBOOKS_DIR / name).read_text(encoding="utf-8"))
    assert data["metadata"]["memtomem"]["profile"] == "minimal-langgraph"
    for cell in data["cells"]:
        if cell["cell_type"] == "code":
            assert cell["execution_count"] is None
            assert cell["outputs"] == []


def test_first_user_notebook_contract() -> None:
    data = json.loads((NOTEBOOKS_DIR / "00_start_here.ipynb").read_text(encoding="utf-8"))
    assert data["metadata"]["memtomem"]["profile"] == "minimal-code"
    for cell in data["cells"]:
        if cell["cell_type"] == "code":
            assert cell["outputs"] == [] and cell["execution_count"] is None
            assert "evaluation.json" not in "".join(cell["source"])


def test_optional_llm_is_explicit_unsaved_preview() -> None:
    data = json.loads((NOTEBOOKS_DIR / OPTIONAL_LLM_NOTEBOOK).read_text(encoding="utf-8"))
    code_cells = [cell for cell in data["cells"] if cell["cell_type"] == "code"]
    source = "".join(code_cells[-1]["source"])
    tree = ast.parse(source)
    opt_in = next(
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "RUN_LLM" for t in node.targets)
    )
    assert isinstance(opt_in.value, ast.Constant) and opt_in.value.value is False
    assert 'base_url="https://api.openai.com/v1"' in source
    assert '"approved": False' in source
    assert "store=False" in source
    assert 'response.status != "completed"' in source
    assert 'os.environ["OPENAI_API_KEY"]' in source
    assert 'os.environ["OPENAI_MODEL"]' in source


def _exception_names(node: ast.expr | None) -> set[str]:
    """Exception names a handler's ``type`` expression catches."""
    if node is None:
        return set()
    parts = node.elts if isinstance(node, ast.Tuple) else [node]
    names = set()
    for part in parts:
        if isinstance(part, ast.Name):
            names.add(part.id)
        elif isinstance(part, ast.Attribute):
            names.add(part.attr)
    return names


def _preflight_modules(source: str, remediation: str) -> set[str]:
    """Modules the cell's leading preflight ``try`` protects.

    Deliberately narrow, because each relaxation is a way for the guard to
    pass on a cell that would *not* fail clearly:

    * only the **first executable statement** qualifies — a ``try`` further
      down (or nested in a function that nobody calls) does not stop the
      setup code above it from running;
    * only direct statements of ``node.body`` are read, so an import behind
      ``if False:`` or inside a nested ``try`` is not credited;
    * an import in an ``except``/``finally`` block does not count — it never
      runs the protected path;
    * exception names are matched exactly, so a handler for some
      ``MyImportErrorWrapper`` does not qualify;
    * the handler must both name *remediation* and actually ``raise`` —
      a swallowed ImportError leaves the notebook to crash later, deep in
      setup, which is precisely what the preflight promises not to do.
    """
    body = ast.parse(source).body
    # Skip a leading docstring/comment-only expression, then require the try.
    lead = [stmt for stmt in body if not isinstance(stmt, ast.Expr)]
    if not lead or not isinstance(lead[0], ast.Try):
        return set()
    node = lead[0]
    remediating = [
        handler
        for handler in node.handlers
        if _exception_names(handler.type) & _IMPORT_EXCEPTIONS
        and remediation in ast.unparse(handler)
        and any(isinstance(inner, ast.Raise) for inner in ast.walk(handler))
    ]
    if not remediating:
        return set()
    modules: set[str] = set()
    for stmt in node.body:
        if isinstance(stmt, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in stmt.names)
        elif isinstance(stmt, ast.ImportFrom) and stmt.module:
            modules.add(stmt.module.split(".")[0])
    return modules


@pytest.mark.parametrize("notebook", _notebook_files(), ids=lambda p: p.stem)
def test_notebook_starts_with_embedding_backend_preflight(notebook: Path) -> None:
    """Every notebook checks its own runtime dependencies before setup."""
    data = json.loads(notebook.read_text(encoding="utf-8"))
    first_code = next((cell for cell in data["cells"] if cell["cell_type"] == "code"), None)
    assert first_code is not None, f"{notebook.name} has no code cell"
    source = "".join(first_code["source"])
    if notebook.name == "00_start_here.ipynb":
        modules = _preflight_modules(source, "memtomem[code]")
        assert {"memtomem", "tree_sitter", "tree_sitter_python"} <= modules
        return
    if notebook.name in BEGINNER_NOTEBOOKS:
        modules = _preflight_modules(source, "memtomem[langgraph]")
        assert {"langgraph", "memtomem"} <= modules
        assert data["metadata"]["memtomem"]["profile"] == "minimal-langgraph"
        return
    modules = _preflight_modules(source, "memtomem[onnx]")
    assert "fastembed" in modules, (
        f"{notebook.name}: the first code cell must import fastembed in the body of a "
        "try whose ModuleNotFoundError/ImportError handler names the 'memtomem[onnx]' "
        f"extra (qualifying imports found: {sorted(modules)})"
    )
