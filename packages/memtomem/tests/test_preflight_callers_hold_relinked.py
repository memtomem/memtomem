"""Every caller of ``preflight_mutation`` also holds a relinked source (#3291).

A preflight that finds its path relinked refuses the write, and the source it
read has to be held whatever the judge said (``hold_relinked_mutation``). That
step lives at each call site, so a new caller that forgets it would refuse the
write and leave the source searchable. This reads the source tree: a function
that references ``preflight_mutation`` must be one of the known callers, and
must reference ``hold_relinked_mutation`` too.
"""

from __future__ import annotations

import ast
from pathlib import Path

import memtomem

_SRC = Path(memtomem.__file__).resolve().parent

# (module path under the package, innermost enclosing function). A new caller
# belongs here, with its hold.
_CALLERS = {
    ("server/tools/memory_crud.py", "_mutate_file_and_reindex"),
    ("tools/memory_mutation.py", "mutate_source_and_reindex"),
    ("tools/memory_mutation.py", "preflight_refusal"),
    ("web/routes/chunks.py", "delete_chunk"),
    ("web/routes/scratch.py", "promote_scratch"),
    ("web/routes/system_indexing.py", "add_memory"),
}


def _attribute_uses(name: str) -> dict[tuple[str, str], int]:
    """``(module, innermost function)`` → how often ``<expr>.name`` appears in it."""
    found: dict[tuple[str, str], int] = {}
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        module = path.relative_to(_SRC).as_posix()

        def visit(node: ast.AST, owner: str) -> None:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                owner = node.name
            if isinstance(node, ast.Attribute) and node.attr == name:
                found[(module, owner)] = found.get((module, owner), 0) + 1
            for child in ast.iter_child_nodes(node):
                visit(child, owner)

        visit(tree, "<module>")
    return found


def test_the_known_callers_are_all_the_callers():
    callers = set(_attribute_uses("preflight_mutation"))
    assert callers == _CALLERS


def test_every_caller_holds_a_relinked_source():
    holders = set(_attribute_uses("hold_relinked_mutation"))
    assert _CALLERS - holders == set()
