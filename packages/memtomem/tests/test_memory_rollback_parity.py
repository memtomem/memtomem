"""Parity guard for the two copies of the memory-CRUD rollback contract (#2347).

``server/tools/memory_crud._mutate_file_and_reindex`` (the MCP tail) and
``tools/memory_mutation.mutate_source_and_reindex`` (the web tail) are two
spellings of one contract, and the whole point of the second copy is that it
carries the same rules. They have drifted before — the cache invalidation
#2141 added landed on the MCP side only — and both carried #2347's two defects
identically because the defect was copied along with the contract.

So the shape is pinned rather than the prose: neither site may write the source
by name, each reads its pre-image and restores it exactly once through the
shared primitives, and the restore runs inside a handler that binds the
exception it is rolling back — the property that keeps a failing restore from
outranking the body's error (the #2229 rule).

Structural, not behavioural: the behaviour is pinned at each site
(``test_memory_crud_concurrency.py``, ``test_memory_mutation.py``) and in the
primitives (``test_memory_writer.py``). This catches the next copy drifting
back to a bare ``write_text``, which no other guard scans for — neither
``test_context_atomic_write_guard`` nor the C0 prelude guard covers ``tools/``
or ``server/tools/memory_crud.py``.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "memtomem"

_SITES = (
    ("server/tools/memory_crud.py", "_mutate_file_and_reindex"),
    ("tools/memory_mutation.py", "mutate_source_and_reindex"),
)

# The primitives the contract is expressed in.
_READ = "read_pre_image"
_RESTORE = "restore_pre_image_quietly"

# Bare writes that would put bytes on the source without the no-create,
# identity-checked open. ``write_text``/``write_bytes`` create; that is the
# resurrection defect.
_BARE_WRITE_ATTRS = frozenset({"write_text", "write_bytes"})


def _function(rel: str, name: str) -> ast.AsyncFunctionDef:
    tree = ast.parse((_SRC / rel).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{rel} no longer defines {name}()")


def _name_count(fn: ast.AST, target: str) -> int:
    return sum(1 for n in ast.walk(fn) if isinstance(n, ast.Name) and n.id == target)


@pytest.mark.parametrize(("rel", "name"), _SITES)
def test_the_site_never_writes_the_source_by_name(rel, name):
    fn = _function(rel, name)

    # Attribute references, not just calls: the shape being guarded is
    # ``asyncio.to_thread(source_file.write_text, ...)``, where the write is an
    # uncalled attribute handed to the executor (the reasoning
    # ``test_context_atomic_write_guard`` records for the same trap).
    bare = [
        f"{rel}:{n.lineno} .{n.attr}"
        for n in ast.walk(fn)
        if isinstance(n, ast.Attribute) and n.attr in _BARE_WRITE_ATTRS
    ]
    assert not bare, (
        f"{name}() writes the source directly ({bare}); the rollback must go "
        f"through {_RESTORE}(), which never creates a file another process "
        "removed and never raises over the error it is rolling back (#2347)"
    )

    creating_opens = [
        f"{rel}:{n.lineno}"
        for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "open"
        and any(isinstance(a, ast.Constant) and str(a.value)[:1] in "wax" for a in n.args[1:])
    ]
    assert not creating_opens, f"{name}() opens the source for writing ({creating_opens})"


@pytest.mark.parametrize(("rel", "name"), _SITES)
def test_the_site_reads_and_restores_exactly_once(rel, name):
    fn = _function(rel, name)

    # Counted as names rather than calls: both are handed to
    # ``asyncio.to_thread`` uncalled.
    assert _name_count(fn, _READ) == 1, f"{name}() must take exactly one pre-image"
    assert _name_count(fn, _RESTORE) == 1, f"{name}() must restore exactly once"


@pytest.mark.parametrize(("rel", "name"), _SITES)
def test_the_restore_runs_inside_the_handler_that_names_the_error(rel, name):
    fn = _function(rel, name)

    handlers = [
        h
        for h in ast.walk(fn)
        if isinstance(h, ast.ExceptHandler) and _name_count(h, _RESTORE) == 1
    ]
    assert len(handlers) == 1, f"{name}()'s restore must sit in exactly one except handler"
    handler = handlers[0]

    assert isinstance(handler.type, ast.Name) and handler.type.id == "Exception", (
        f"{name}()'s rollback handler must catch Exception"
    )
    # Bound, because the outcome has to be reported *with* the cause: the
    # defect #2347 closed was exactly the cause going missing.
    assert handler.name, f"{name}()'s rollback handler must bind the exception it is rolling back"


def test_both_sites_still_exist_under_these_names():
    """A rename must not hollow this file out into vacuous parametrization."""
    from memtomem.server.tools import memory_crud
    from memtomem.tools import memory_mutation

    assert callable(memory_crud._mutate_file_and_reindex)
    assert callable(memory_mutation.mutate_source_and_reindex)
    for module in (memory_crud, memory_mutation):
        assert callable(getattr(module, _READ))
        assert callable(getattr(module, _RESTORE))
