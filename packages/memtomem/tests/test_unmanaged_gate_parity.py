"""Every by-id rewrite of a chunk's source asks ``is_managed_source`` first.

A chunk's ``source_file`` can name a file outside every memory directory, so
each function that locks or rewrites that path has to ask where it is.
``test_unmanaged_source_mutation.py`` proves the functions that exist today
do. This file is for the next one: a new function that asks the read-only
question, references a span writer or takes ``locked_source_chunk`` fails here
until somebody decides which kind it is.

Three ways a by-id site is found, because no single one finds them all:

* it asks ``is_read_only_source``, the question every such site already asks;
* it references a span writer from ``tools/memory_writer.py``;
* it calls ``locked_source_chunk``, which refuses by returning a reason.

What is checked is presence, count and line order of calls by name. Not
checked: which path a call is given, whether its answer is used, or whether a
refusal leaves the function before the lock. A call on the wrong path, or one
whose result is dropped, satisfies this file; the behaviour tests are what
would fail.
"""

from __future__ import annotations

import ast
import functools
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "memtomem"

_GATE = "is_managed_source"
_READ_ONLY_GATE = "is_read_only_source"
#: The functions that rewrite a line span of an existing file.
_WRITERS = {"replace_chunk_body", "remove_lines", "replace_lines"}
_WRITER_HOME = "tools/memory_writer.py"
#: The helpers that lock a stored chunk's source and refuse before they do.
_LOCK_HELPERS = {"_locked_chunk", "locked_source_chunk"}
_ACQUIRES = {"async_file_lock", "async_memory_file_lock", "locked_source_chunk"}
_REASON = "unmanaged"

#: Functions that ask the read-only question about a file a *stored chunk*
#: names. Each must ask ``is_managed_source`` as often.
_BY_ID = {
    ("server/tools/memory_crud.py", "_locked_chunk"),
    ("server/tools/memory_crud.py", "_mutate_file_and_reindex"),
    ("tools/memory_mutation.py", "locked_source_chunk"),
    ("tools/memory_mutation.py", "mutate_source_and_reindex"),
    ("web/routes/chunks.py", "get_chunk_source"),
    ("web/routes/chunks.py", "edit_chunk"),
    ("web/routes/chunks.py", "delete_chunk"),
}

#: Functions that ask it about something else, and what. None of them takes
#: its path from a stored chunk.
_NOT_BY_ID = {
    ("indexing/engine.py", "chunk_content"): "stamps source_read_only while indexing",
    ("source_provenance.py", "refuse_replace_target"): "a replace target its caller names",
    ("server/tools/memory_crud.py", "_mem_add_core"): "the file an add appends to",
    ("server/tools/memory_crud.py", "mem_batch_add"): "the file a batch add appends to",
    ("cli/memory.py", "_add"): "the file an add appends to",
    ("cli/shell.py", "_cmd_add"): "the file an add appends to",
    ("cli/agent_cmd.py", "_run_share"): "the file a share appends to",
    ("cli/review_cmd.py", "_decide"): "the daily file an approved draft is appended to",
    ("integrations/langgraph.py", "add"): "the file an add appends to",
    ("web/routes/system_indexing.py", "add_memory"): "the file an add appends to",
    ("web/routes/system_indexing.py", "upload_files"): "the upload directory",
    ("web/upload_quarantine.py", "promote_no_overwrite"): "an upload's destination",
    ("web/routes/scratch.py", "promote_scratch"): "the file a promoted scratch entry lands in",
}


def _functions(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _attr_calls(node: ast.AST, attr: str) -> list[int]:
    """Lines of ``<anything>.<attr>(...)`` calls under ``node``."""
    return sorted(
        n.lineno
        for n in ast.walk(node)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == attr
    )


def _name_calls(node: ast.AST, names: set[str]) -> list[int]:
    """Lines of ``<name>(...)`` calls under ``node``, bare or as an attribute."""
    out = []
    for n in ast.walk(node):
        if not isinstance(n, ast.Call):
            continue
        func = n.func
        if isinstance(func, ast.Name) and func.id in names:
            out.append(n.lineno)
        elif isinstance(func, ast.Attribute) and func.attr in names:
            out.append(n.lineno)
    return sorted(out)


def _references(node: ast.AST, names: set[str]) -> bool:
    """Whether ``node`` mentions one of ``names`` at all: called, or passed on."""
    for n in ast.walk(node):
        if isinstance(n, ast.Name) and n.id in names:
            return True
        if isinstance(n, ast.Attribute) and n.attr in names:
            return True
    return False


def _compares_to(node: ast.AST, literal: str) -> bool:
    for n in ast.walk(node):
        if isinstance(n, ast.Compare):
            for side in (n.left, *n.comparators):
                if isinstance(side, ast.Constant) and side.value == literal:
                    return True
    return False


def _order_offences(fn: ast.AST, where: str) -> list[str]:
    """The gate must come before the lock it keeps off the file, loop by loop.

    Taking the lock creates ``.<name>.lock`` beside the source, so a refusal
    decided after it has already written there. A gate before a loop does not
    cover an acquire inside it: the loop re-keys the path and acquires again.
    Judged by line number: a gate call on an earlier line, in the same loop.
    """
    acquires = _name_calls(fn, _ACQUIRES)
    gates = _attr_calls(fn, _GATE)
    if not acquires:
        return []
    if not gates or min(gates) > min(acquires):
        return [f"{where}: no {_GATE} before the lock acquire at line {min(acquires)}"]
    out = []
    for loop in ast.walk(fn):
        if not isinstance(loop, (ast.For, ast.AsyncFor, ast.While)):
            continue
        in_loop = _name_calls(loop, _ACQUIRES)
        if in_loop and not any(g < min(in_loop) for g in _attr_calls(loop, _GATE)):
            out.append(f"{where}: lock acquired in a loop (line {min(in_loop)}) with no gate in it")
    return out


@functools.cache
def _source_functions() -> tuple[tuple[str, ast.AST], ...]:
    found = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        found.extend((rel, fn) for fn in _functions(tree))
    return tuple(found)


def _read_only_askers() -> dict[tuple[str, str], ast.AST]:
    return {
        (rel, fn.name): fn
        for rel, fn in _source_functions()
        # The engine and its snapshot define the predicate; they do not ask it.
        if _attr_calls(fn, _READ_ONLY_GATE) and fn.name != _READ_ONLY_GATE
    }


def test_every_registry_entry_names_a_function_that_asks():
    """A misspelled entry classifies nothing and the file stays green."""
    missing = (_BY_ID | set(_NOT_BY_ID)) - set(_read_only_askers())
    assert not missing, f"registry entries that no longer ask {_READ_ONLY_GATE}: {sorted(missing)}"


def test_every_function_that_asks_the_read_only_question_is_classified():
    unclassified = set(_read_only_askers()) - _BY_ID - set(_NOT_BY_ID)
    assert not unclassified, (
        f"these functions ask {_READ_ONLY_GATE} and are in neither _BY_ID nor _NOT_BY_ID: "
        f"{sorted(unclassified)}. If the path comes from a stored chunk, it is _BY_ID and "
        f"must ask {_GATE} too."
    )


@pytest.mark.parametrize("site", sorted(_BY_ID), ids=lambda s: f"{s[0]}:{s[1]}")
def test_a_by_id_site_asks_both_questions_equally_often(site):
    """As many calls of one name as of the other; the arguments are not compared."""
    fn = _read_only_askers()[site]
    read_only, managed = _attr_calls(fn, _READ_ONLY_GATE), _attr_calls(fn, _GATE)
    assert len(managed) == len(read_only), (
        f"{site[0]}:{site[1]} asks {_READ_ONLY_GATE} at lines {read_only} "
        f"and {_GATE} at lines {managed}"
    )


@pytest.mark.parametrize("site", sorted(_BY_ID), ids=lambda s: f"{s[0]}:{s[1]}")
def test_the_gate_precedes_the_lock(site):
    offences = _order_offences(_read_only_askers()[site], f"{site[0]}:{site[1]}")
    assert not offences, "\n".join(offences)


def test_every_function_that_references_a_span_writer_is_gated():
    """Asks the question itself, or locks through a helper that does."""
    seen, ungated = [], []
    for rel, fn in _source_functions():
        if rel == _WRITER_HOME or not _references(fn, _WRITERS):
            continue
        seen.append((rel, fn.name))
        if not _attr_calls(fn, _GATE) and not _name_calls(fn, _LOCK_HELPERS):
            ungated.append((rel, fn.name))
    assert ("server/tools/memory_crud.py", "mem_edit") in seen, "the matcher sees no rewriter"
    assert ("web/routes/chunks.py", "delete_chunk") in seen, "the matcher misses a passed writer"
    assert not ungated, (
        f"these functions reference a span writer with no {_GATE} and no lock helper: {ungated}"
    )


def test_the_lock_helpers_are_by_id_sites():
    """The rule above leans on them, so they must be among the gated ones."""
    helpers = {site for site in _BY_ID if site[1] in _LOCK_HELPERS}
    assert {name for _rel, name in helpers} == _LOCK_HELPERS


def test_every_caller_of_locked_source_chunk_handles_the_refusal():
    """The helper reports ``"unmanaged"``; a caller that ignores it reads it as a miss."""
    callers = [
        (rel, fn.name, _compares_to(fn, _REASON))
        for rel, fn in _source_functions()
        if _name_calls(fn, {"locked_source_chunk"}) and fn.name != "locked_source_chunk"
    ]
    assert ("integrations/langgraph.py", "delete", True) in callers, callers
    assert len(callers) >= 3, f"expected the two web routes and the LangGraph delete: {callers}"
    unhandled = [(rel, name) for rel, name, handled in callers if not handled]
    assert not unhandled, f'no comparison against "{_REASON}" in: {unhandled}'


# --- the matchers, on shapes written for them --------------------------------

_GATE_FIRST = """
async def f(engine, target):
    if not engine.is_managed_source(target):
        return
    async with async_memory_file_lock(target):
        pass
"""

_LOCK_FIRST = """
async def f(engine, target):
    async with async_memory_file_lock(target):
        if not engine.is_managed_source(target):
            return
"""

_NO_GATE = """
async def f(engine, target):
    async with locked_source_chunk(target):
        pass
"""

_GATE_OUTSIDE_LOOP = """
async def f(engine, target):
    if not engine.is_managed_source(target):
        return
    for _ in range(3):
        async with async_memory_file_lock(target):
            target = other()
"""

_GATE_INSIDE_LOOP = """
async def f(engine, target):
    for _ in range(3):
        if not engine.is_managed_source(target):
            return
        async with async_memory_file_lock(target):
            target = other()
"""

_GATE_AFTER_LOCK_IN_LOOP = """
async def f(engine, target):
    if not engine.is_managed_source(target):
        return
    for _ in range(3):
        async with async_memory_file_lock(target):
            if not engine.is_managed_source(target):
                return
"""


@pytest.mark.parametrize(
    ("source", "flagged", "why"),
    [
        (_GATE_FIRST, False, "gate before the acquire"),
        (_LOCK_FIRST, True, "gate inside the lock body"),
        (_NO_GATE, True, "a lock helper with no gate at all"),
        (_GATE_OUTSIDE_LOOP, True, "gate outside a loop that re-keys and re-acquires"),
        (_GATE_INSIDE_LOOP, False, "gate inside the loop, before the acquire"),
        (_GATE_AFTER_LOCK_IN_LOOP, True, "the only in-loop gate comes after the acquire"),
    ],
)
def test_the_order_matcher_judges_written_shapes(source, flagged, why):
    (fn,) = _functions(ast.parse(source))
    assert bool(_order_offences(fn, "f")) is flagged, why


def test_the_reference_matcher_sees_a_writer_passed_as_an_argument():
    (fn,) = _functions(ast.parse("async def f():\n    await to_thread(remove_lines, path, 1, 2)\n"))
    assert _references(fn, _WRITERS)
    (fn,) = _functions(ast.parse("def f():\n    return memory_writer.replace_chunk_body\n"))
    assert _references(fn, _WRITERS)
    (fn,) = _functions(ast.parse("def f():\n    return remove_lines_later\n"))
    assert not _references(fn, _WRITERS)


def test_the_reason_matcher_wants_a_comparison():
    (fn,) = _functions(ast.parse('def f(reason):\n    return reason == "unmanaged"\n'))
    assert _compares_to(fn, _REASON)
    (fn,) = _functions(ast.parse('def f(reason):\n    log("unmanaged")\n'))
    assert not _compares_to(fn, _REASON)
