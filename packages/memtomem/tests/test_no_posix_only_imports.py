"""Architectural guard: no module-level POSIX-only imports in src/memtomem/.

A single ``import fcntl`` at module scope crashes ``mm`` on Windows at
CLI registration time — the import resolves before any ``--help`` parse,
before the lazy-import dispatch in ``cli/__init__.py:_register`` ever
runs. PR #623 introduced exactly this regression by hoisting
``import fcntl`` into ``context/_atomic.py``; powerzist reported it as
issue #625, the second time the same shape of bug surfaced (first was
issue #448 in ``cli/uninstall_cmd.py``).

The previous defence was a regex pin in ``test_uninstall_cmd.py`` that
scanned a single file. PR #623's regression slipped past it because
``_atomic.py`` was outside that file's scope. This test replaces the
single-file pin with an AST scan parametrized over every Python module
under ``packages/memtomem/src/memtomem/``, rejecting any of the standard
library's POSIX-only modules at module scope:

  fcntl    pwd    grp    termios    resource

Lazy imports inside functions / methods / nested ``if sys.platform``
branches do not appear at module scope and are therefore allowed. That
is the documented pattern for ``server/__init__.py``, which keeps
``fcntl`` (server is POSIX-only by design — no SIGTERM, different pid
semantics on Windows — but the module is sometimes import-walked from
Windows tooling, so the imports stay lazy).

The negative pin (``test_scan_rejects_module_level_fcntl_in_fixture``)
proves the assertion is symmetric: the scan logic must fail on a file
that contains the forbidden pattern, not just pass on the current tree.
Without that, an off-by-one bug in the parser walk would silently make
this whole guard a no-op.
"""

from __future__ import annotations

import ast
import pathlib
import textwrap

import pytest


POSIX_ONLY_STDLIB = frozenset({"fcntl", "pwd", "grp", "termios", "resource"})

_SRC_ROOT = pathlib.Path(__file__).resolve().parent.parent / "src" / "memtomem"


def _root_module(name: str | None) -> str:
    """``memtomem.context._atomic`` -> ``memtomem``; ``fcntl`` -> ``fcntl``."""
    if not name:
        return ""
    return name.split(".", 1)[0]


def _module_level_violations(tree: ast.Module) -> list[tuple[int, str]]:
    """Return ``(lineno, offending_name)`` for every module-scope POSIX-only
    import in ``tree``. Walks ``tree.body`` only — anything inside a
    ``FunctionDef``, ``AsyncFunctionDef``, ``ClassDef``, or any other nested
    block is module-scope-invisible by definition and therefore allowed.
    """
    violations: list[tuple[int, str]] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = _root_module(alias.name)
                if root in POSIX_ONLY_STDLIB:
                    violations.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            root = _root_module(node.module)
            if root in POSIX_ONLY_STDLIB:
                violations.append((node.lineno, node.module or ""))
        # Note: ``ast.If`` and other compound nodes nest their body items.
        # We deliberately do NOT recurse — a top-level ``if sys.platform``
        # block whose body imports POSIX-only modules is technically
        # module-scope-executed, but that pattern was the source of #625's
        # crash even with the gate (the import statement itself parses
        # before the gate runs). The whole point of #625 is that no such
        # imports should exist at module scope, gated or not — they go
        # inside functions instead.
    return violations


_PY_FILES = sorted(_SRC_ROOT.rglob("*.py"))
assert _PY_FILES, f"AST scan found no .py files under {_SRC_ROOT}"


@pytest.mark.parametrize(
    "py_file",
    _PY_FILES,
    ids=lambda p: str(p.relative_to(_SRC_ROOT)),
)
def test_no_module_level_posix_only_imports(py_file: pathlib.Path) -> None:
    """Positive pin: every file under ``src/memtomem/`` parses to zero
    module-level POSIX-only imports. Catches the #623 / #625 regression
    shape across all files, not just the one site the prior regex pin
    covered.
    """
    src = py_file.read_text(encoding="utf-8")
    tree = ast.parse(src, filename=str(py_file))
    violations = _module_level_violations(tree)
    assert violations == [], (
        f"{py_file.relative_to(_SRC_ROOT)} has module-level POSIX-only "
        f"import(s): {violations}. Move the import inside a function, "
        f"method, or guarded ``sys.platform`` branch — see "
        f"``server/__init__.py`` for the lazy-import pattern."
    )


def test_scan_rejects_module_level_fcntl_in_fixture(tmp_path: pathlib.Path) -> None:
    """Negative pin: hand the scan logic a file with a forbidden import
    and assert it fails. Without this, an off-by-one in
    ``_module_level_violations`` would silently make the positive pin a
    no-op (every file would walk to ``violations == []`` regardless of
    contents).
    """
    bad_file = tmp_path / "fake_module.py"
    bad_file.write_text(
        textwrap.dedent(
            """\
            import os
            import fcntl  # this is the forbidden line
            """
        ),
        encoding="utf-8",
    )

    tree = ast.parse(bad_file.read_text(encoding="utf-8"))
    violations = _module_level_violations(tree)
    assert violations == [(2, "fcntl")], violations


def test_scan_allows_lazy_imports_inside_functions(tmp_path: pathlib.Path) -> None:
    """Negative pin (other direction): a file that imports ``fcntl``
    inside a function body is fine — that is the explicit pattern
    ``server/__init__.py:main`` uses, and the whole point of the guard
    is to NOT flag it.
    """
    good_file = tmp_path / "lazy_import_module.py"
    good_file.write_text(
        textwrap.dedent(
            """\
            import os

            def acquire_pid_lock():
                import fcntl  # lazy — only evaluated on POSIX call paths
                fcntl.flock(0, fcntl.LOCK_EX)
            """
        ),
        encoding="utf-8",
    )

    tree = ast.parse(good_file.read_text(encoding="utf-8"))
    violations = _module_level_violations(tree)
    assert violations == [], violations


def test_scan_catches_from_imports(tmp_path: pathlib.Path) -> None:
    """Negative pin: ``from fcntl import flock`` at module scope is the
    same regression shape — must also fail.
    """
    bad_file = tmp_path / "from_import_module.py"
    bad_file.write_text("from fcntl import flock\n", encoding="utf-8")

    tree = ast.parse(bad_file.read_text(encoding="utf-8"))
    violations = _module_level_violations(tree)
    assert violations == [(1, "fcntl")], violations
