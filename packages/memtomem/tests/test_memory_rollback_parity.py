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


# --------------------------------------------------------------------------
# #2367: the same no-create rule, one layer earlier — on the writer module
# itself. The guard above pins the two rollback tails; nothing pinned the
# forward helpers those tails call, and that is where the second copy of the
# defect lived.

_WRITER = "tools/memory_writer.py"

#: The functions in ``memory_writer`` that are *meant* to create. Appending a
#: note to a file that need not exist yet is what ``mem_add`` does, so the rule
#: is an allowlist rather than a blanket ban — and an allowlist small enough to
#: read is the point. ``append_entry`` delegates here rather than opening.
_CREATORS = frozenset({"append_blocks"})

_CREATING_MODE_CHARS = "wax"


def _writer_functions() -> dict[str, ast.AST]:
    tree = ast.parse((_SRC / _WRITER).read_text(encoding="utf-8"))
    return {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _creating_writes(fn: ast.AST) -> list[str]:
    """The creating writes *fn* spells out literally.

    Recognised: ``.write_text`` / ``.write_bytes`` (as attribute references,
    called or not), a literal ``O_CREAT`` flag, and ``open`` / ``Path.open``
    with a literal creating mode — positional or ``mode=``.

    Not recognised, and deliberately so: a mode computed at runtime, an aliased
    or re-exported ``open``, ``touch()``, a copy or rename that lands on the
    path, or delegation to a helper that creates. This is a regression check on
    the shapes this module has actually used, not a proof that nothing here can
    create a file — read it as a tripwire, and do not take its silence for an
    audit.
    """
    found = []
    for n in ast.walk(fn):
        # ``write_text`` / ``write_bytes`` create — the #2367 defect verbatim.
        # Attribute references rather than calls, for the reason recorded above:
        # the value can be handed to an executor uncalled.
        if isinstance(n, ast.Attribute) and n.attr in _BARE_WRITE_ATTRS:
            found.append(f"line {n.lineno}: .{n.attr}")
        if isinstance(n, ast.Attribute) and n.attr == "O_CREAT":
            found.append(f"line {n.lineno}: O_CREAT")
        if not isinstance(n, ast.Call):
            continue
        # The mode's position depends on the call's shape: builtin ``open``
        # takes the path first, while a bound ``path.open`` already has it, so
        # reading from a fixed index misses ``path.open("w")`` — and reading
        # every argument would take a filename like ``"data.md"`` for a mode.
        if isinstance(n.func, ast.Name) and n.func.id == "open":
            positional = n.args[1:2]
        elif isinstance(n.func, ast.Attribute) and n.func.attr == "open":
            positional = n.args[0:1]
        else:
            continue
        modes = [a.value for a in positional if isinstance(a, ast.Constant)]
        modes += [
            kw.value.value
            for kw in n.keywords
            if kw.arg == "mode" and isinstance(kw.value, ast.Constant)
        ]
        # Containment, not the first character: ``"r+"`` creates nothing while
        # ``"a+"`` and ``"xb"`` both do.
        if any(c in str(m) for m in modes for c in _CREATING_MODE_CHARS):
            found.append(f"line {n.lineno}: open(..., {modes!r})")
    return found


#: One witness per recognised shape, plus the near-misses a looser matcher gets
#: wrong. A guard whose own recogniser goes untested passes just as happily
#: once it has stopped recognising anything.
_GUARD_WITNESSES = (
    ('p.write_text("x")', True),
    ('p.write_bytes(b"x")', True),
    ("os.open(p, os.O_CREAT)", True),
    ('open(p, "w")', True),
    ('open(p, "a+")', True),
    ('p.open("w")', True),
    ('p.open(mode="x")', True),
    ('open(p, "r+")', False),
    ('p.open("r+")', False),
    # The filename carries an "a"; only the mode position may be read as one.
    ('open("data.md", "r")', False),
)


@pytest.mark.parametrize(("source", "creates"), _GUARD_WITNESSES)
def test_the_creating_write_recogniser_reads_each_shape(source, creates):
    fn = ast.parse(f"def f(p):\n    {source}\n").body[0]

    assert bool(_creating_writes(fn)) is creates, source


def test_only_the_declared_creators_in_memory_writer_may_create():
    """No function outside the allowlist may bring the source file into being.

    Scope comes from the module's own AST rather than a hand-written list of
    edit helpers: a helper added later is covered the day it is written, which
    a list of the three that exist today would not be.
    """
    functions = _writer_functions()
    assert _CREATORS <= functions.keys(), (
        f"the creator allowlist names functions that no longer exist: "
        f"{sorted(_CREATORS - functions.keys())}"
    )

    offenders = {
        name: hits
        for name, fn in functions.items()
        if name not in _CREATORS and (hits := _creating_writes(fn))
    }
    assert not offenders, (
        f"{_WRITER}: {offenders} can create the file it was asked to edit. A "
        "rewrite must open the existing file without O_CREAT so a source "
        "removed mid-span answers ENOENT instead of being resurrected with "
        "the edit applied (#2367)."
    )


def test_the_declared_creator_really_creates():
    """The allowlist must name a function that creates, not a stale name.

    Without this the guard above passes just as well after ``append_blocks``
    stops creating — at which point the allowlist silently exempts nothing and
    nobody notices the entry is dead.
    """
    fn = _writer_functions()["append_blocks"]
    assert _creating_writes(fn), (
        "append_blocks no longer creates; it is on the creator allowlist "
        "precisely because mem_add's append must create its target file"
    )
