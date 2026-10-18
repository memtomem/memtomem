"""Every ``index_file`` call outside the engine is classified, and keeps its class's rule.

The engine judges the whole file on every index, so no caller can skip the
redaction gate. What a caller *can* still get wrong is the valve: a memory
tool that forwards its ``force_unsafe`` lets it cover the whole file. Two
shapes are safe, and this registry pins each call site to one of them:

``modify``
    Appends to or edits an existing file under its lock. It must ask the
    engine about the file before writing (``preflight_mutation``, directly or
    through ``preflight_refusal``), which is what limits the valve to a file
    that held nothing flagged.
``replace``
    Writes a whole file from text it guarded, then indexes it.

Every call that forwards a valve binds it with ``valve_text`` to the text the
file should hold: the guarded text for ``replace``, the judged file plus the
write for ``modify``. The engine drops an unbound valve on an
``ingress_guarded`` call anyway; this pins that no caller relies on that.
``plain``
    Bulk, watcher and maintenance indexes: no ingress, no forwarded valve.

A new call site fails here until it is classified; a call that disappears
fails until its entry is removed.
"""

from __future__ import annotations

import ast
from pathlib import Path

import memtomem

SRC = Path(memtomem.__file__).parent

REGISTRY = {
    "cli/agent_cmd.py::_run_share": "modify",
    "cli/memory.py::_add": "modify",
    "cli/review_cmd.py::_decide": "modify",
    "cli/shell.py::_cmd_add": "modify",
    "integrations/langgraph.py::add": "modify",
    "server/tools/memory_crud.py::_mutate_file_and_reindex": "modify",
    "server/tools/memory_crud.py::_mem_add_core": "modify",
    "server/tools/memory_crud.py::mem_batch_add": "modify",
    "tools/memory_mutation.py::mutate_source_and_reindex": "modify",
    "web/routes/chunks.py::delete_chunk": "modify",
    "web/routes/scratch.py::promote_scratch": "modify",
    "web/routes/system.py::add_memory": "modify",
    "server/tools/importers.py::mem_import_notion": "replace",
    "server/tools/importers.py::mem_import_obsidian": "replace",
    "server/tools/session.py::_write_summary_archive": "replace",
    "server/tools/url_index.py::mem_fetch": "replace",
    "web/routes/system.py::upload_files": "replace",
    "cli/indexing.py::_make_indexer._do": "plain",
    "cli/ingest_cmd.py::_ingest_files_with_components": "plain",
    "indexing/budget_migration.py::apply_plan": "plain",
    "indexing/watcher.py::_recheck_held_loop": "plain",
    "indexing/watcher.py::_reindex": "plain",
}

_PREFLIGHT_NAMES = {"preflight_mutation", "preflight_refusal", "_preflight_refusal"}


def _sites() -> list[tuple[str, ast.Call, ast.AST]]:
    """``(key, call, outermost function)`` for every ``index_file`` call."""
    found: list[tuple[str, ast.Call, ast.AST]] = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if rel == "indexing/engine.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))

        def walk(node: ast.AST, stack: list[ast.AST]) -> None:
            for child in ast.iter_child_nodes(node):
                inner = stack
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    inner = [*stack, child]
                if (
                    isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Attribute)
                    and child.func.attr == "index_file"
                ):
                    names = ".".join(f.name for f in inner)  # type: ignore[attr-defined]
                    found.append((f"{rel}::{names}", child, inner[0] if inner else tree))
                walk(child, inner)

        walk(tree, [])
    return found


def _keywords(call: ast.Call) -> dict[str, ast.expr]:
    return {k.arg: k.value for k in call.keywords if k.arg}


def test_no_caller_passes_the_removed_skip():
    offenders = [key for key, call, _ in _sites() if "already_scanned" in _keywords(call)]
    assert offenders == []
    assert all("already_scanned" not in p.read_text(encoding="utf-8") for p in SRC.rglob("*.py"))


def test_every_call_site_is_registered_and_every_entry_is_live():
    keys = {key for key, _, _ in _sites()}
    assert sorted(keys - REGISTRY.keys()) == [], "classify the new index_file call site"
    assert sorted(REGISTRY.keys() - keys) == [], "remove the entry for a call that is gone"


def _referenced_names(node: ast.AST) -> set[str]:
    """Every attribute and bare name under ``node``.

    A reference, not only a call: the tools hand the method to
    ``asyncio.to_thread`` rather than calling it inline.
    """
    names: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Attribute):
            names.add(sub.attr)
        elif isinstance(sub, ast.Name):
            names.add(sub.id)
    return names


def test_modify_sites_ask_the_engine_before_writing():
    missing = [
        key
        for key, _, outer in _sites()
        if REGISTRY.get(key) == "modify" and not _referenced_names(outer) & _PREFLIGHT_NAMES
    ]
    assert missing == []


def test_every_forwarded_valve_is_bound_to_text():
    """A memory tool's valve names the bytes it covers, at every call site."""
    unbound = []
    for key, call, _ in _sites():
        kws = _keywords(call)
        forwards = "force_unsafe" in kws and not (
            isinstance(kws["force_unsafe"], ast.Constant) and kws["force_unsafe"].value is False
        )
        if forwards and "valve_text" not in kws:
            unbound.append(key)
    assert unbound == []


def test_plain_sites_forward_no_valve():
    forwarding = [
        key
        for key, call, _ in _sites()
        if REGISTRY.get(key) == "plain"
        and {"force_unsafe", "ingress_guarded"} & _keywords(call).keys()
    ]
    assert forwarding == []
