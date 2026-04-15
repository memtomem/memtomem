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
