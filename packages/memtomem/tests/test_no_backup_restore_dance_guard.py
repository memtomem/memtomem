"""Guard: a test may not back up a file to a local and restore it in a ``finally``.

This bans the exact shape that destroyed a real ``~/.claude/settings.json`` in
issue #1892::

    target = Path.home() / ".claude" / "settings.json"
    backup = target.read_text(encoding="utf-8") if target.is_file() else None
    try:
        target.write_text(...)          # the test's real work
        ...
    finally:
        if backup is not None:
            target.write_text(backup)   # the only copy lived in a local
        elif target.is_file():
            target.unlink()

The bug is not the write — it is that the file's only copy is a local variable.
A ``pkill``, a CI job timeout, or an IDE stop button does not run ``finally``,
and the copy dies with the process. In #1892 the next test then read the wreckage
as *its* backup and faithfully restored that, so the loss laundered itself into
what looked like a stable state: the residue is valid JSON, so nothing errored.

The remedy is never "make the restore more robust" — it is to not touch the real
file. Point ``HOME`` at a throwaway directory with ``tests/helpers.py:set_home``
(that is how #1893 fixed the 20 sites), or operate on ``tmp_path``. Then there is
nothing to restore.

Scope — this is a **regression-shape ban, not a general invariant.** It catches
the spelling that caused #1892 and shapes near it. It does NOT catch every way a
test can damage a real file, and deliberately does not pretend to:

* a backup bound by a fixture or a helper the scanner cannot follow
* ``with target.open() as f: backup = f.read()``
* a restore whose destination is computed rather than named

The complementary half — a test that never names a home path at all but calls
production code that resolves one, which is how #1892 actually reached the file —
is a runtime concern and cannot be seen statically at any granularity. See the
measurement note below.

Why not a broader static rule: "a test touching a home path must redirect home"
was evaluated and rejected on measurement. At module granularity it **passes**
the commit that destroyed the file (two correct ``set_home`` calls elsewhere in
the module vouch for twenty destructive ones); at function granularity it
**fails** the commit that fixed it (the fix lives in an autouse fixture, a
different function from all twenty use sites). Do not revive it.

Pattern lineage: ``feedback_ast_architectural_guard_pattern.md``. Registry idiom
and the empty-allowlist precedent follow ``test_context_atomic_write_guard.py``.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest

_TESTS_ROOT = Path(__file__).resolve().parent

#: Attribute names that put a file back the way it was.
_RESTORE_ATTRS = frozenset({"write_text", "write_bytes", "unlink", "replace", "rename"})

#: ``(module, function)`` calls that copy or move a file over another.
_RESTORE_FUNCS = frozenset(
    {
        ("shutil", "copy"),
        ("shutil", "copy2"),
        ("shutil", "copyfile"),
        ("shutil", "move"),
        ("os", "replace"),
        ("os", "rename"),
    }
)

#: Attribute names that read a whole file into a value — the "backup" half.
_BACKUP_READS = frozenset({"read_text", "read_bytes"})

#: ``(path relative to tests/, enclosing function)`` pairs allowed to keep a
#: backup/restore dance. Empty by design — the fix is always to sandbox the home
#: (``helpers.set_home``) or use ``tmp_path``, never to restore more carefully.
#: Add an entry ONLY with an inline why, mirroring the DEFERRED registry
#: convention in ``test_validate_namespace_architectural_guard.py``.
ALLOWED_BACKUP_RESTORE: frozenset[tuple[str, str]] = frozenset()


#: Scopes whose bodies belong to *them*, not to the enclosing function.
_NESTED_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


def _walk_own_scope(node: ast.AST):
    """Yield descendants of ``node`` in its OWN lexical scope.

    Unlike ``ast.walk``, does not descend into nested ``def`` / ``lambda`` /
    ``class`` bodies. Without this, a backup bound in an outer function pairs
    with an inner function's ``try/finally`` and the offender is reported against
    the wrong function — and an inner helper that never runs would incriminate
    its parent. Same helper as ``test_web_invariants_registry.py:426``.
    """
    for child in ast.iter_child_nodes(node):
        yield child
        if not isinstance(child, _NESTED_SCOPES):
            yield from _walk_own_scope(child)


@dataclass(frozen=True)
class _Backup:
    """A local holding a file's contents, and the path it came from."""

    name: str | None
    source: str | None


def _name_of(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return _name_of(node.value)
    return None


def _binds_a_backup(node: ast.AST) -> _Backup | None:
    """The backup a statement binds, if any.

    Shapes, all of which appear in or next to the #1892 code:

    * ``backup = target.read_text(...)`` / ``.read_bytes(...)``, including the
      ``... if target.is_file() else None`` conditional form actually used.
    * ``backup: str = target.read_text(...)`` — annotated assignment.
    * ``backup = await asyncio.to_thread(target.read_text)`` — the offloaded
      read, which pairs naturally with the offloaded restore below.
    * ``shutil.copy2(target, backup_path)`` — statement form, no assignment.
    """
    targets: list[ast.expr] = []
    value: ast.expr | None = None
    if isinstance(node, ast.Assign):
        targets, value = list(node.targets), node.value
    elif isinstance(node, ast.AnnAssign) and node.value is not None:
        targets, value = [node.target], node.value

    if value is not None:
        for sub in ast.walk(value):
            source: str | None = None
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute):
                if sub.func.attr in _BACKUP_READS:
                    source = _name_of(sub.func.value)
            elif isinstance(sub, ast.Attribute) and sub.attr in _BACKUP_READS:
                # ``asyncio.to_thread(target.read_text)`` — a reference, not a call.
                source = _name_of(sub.value)
            if source is not None:
                return _Backup(name=_name_of(targets[0]) if targets else None, source=source)
        return None

    if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
        call = node.value
        if isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Name):
            if (call.func.value.id, call.func.attr) in _RESTORE_FUNCS and len(call.args) >= 2:
                # copy(src, dst) — the spare copy is the destination.
                return _Backup(name=_name_of(call.args[1]), source=_name_of(call.args[0]))
    return None


def _restores_in_finally(finalbody: list[ast.stmt]) -> list[tuple[str, set[str]]]:
    """``(description, names involved)`` for restore-shaped ops in a ``finally``."""
    found: list[tuple[str, set[str]]] = []
    for stmt in finalbody:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                func = node.func
                names = {n for n in (_name_of(a) for a in node.args) if n}
                if isinstance(func.value, ast.Name):
                    if (func.value.id, func.attr) in _RESTORE_FUNCS and names:
                        found.append((f"{func.value.id}.{func.attr}", names))
                        continue
                if func.attr in _RESTORE_ATTRS:
                    receiver = _name_of(func.value)
                    if receiver:
                        found.append((f"{receiver}.{func.attr}", names | {receiver}))
                    continue
                # ``asyncio.to_thread(target.write_text, backup)`` — the restore
                # is an argument, never the func of a Call. The repo has been
                # bitten by exactly this (test_context_atomic_write_guard.py:74).
                for arg in node.args:
                    if isinstance(arg, ast.Attribute) and arg.attr in _RESTORE_ATTRS:
                        receiver = _name_of(arg.value)
                        if receiver:
                            found.append((f"{receiver}.{arg.attr}", names | {receiver}))
    return found


def backup_restore_dances(tree: ast.AST) -> list[tuple[str, int, str]]:
    """``(function, lineno, what)`` for each backup-then-restore-in-finally shape.

    Three conditions, all required — each one removes a class of false positive:

    1. the function binds a backup before the ``try`` (so ordinary
       ``tmp.unlink()`` cleanup is not flagged),
    2. the ``finally`` performs a restore-shaped operation, and
    3. that restore **mentions the backup or the path it came from**. Without (3)
       any earlier ``.read_text()`` pairs with any later unrelated cleanup — e.g.
       reading a golden fixture, then unlinking a scratch file in ``finally``.
    """
    offenders: list[tuple[str, int, str]] = []
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in _walk_own_scope(func):
            if not isinstance(node, ast.Try) or not node.finalbody:
                continue
            restores = _restores_in_finally(node.finalbody)
            if not restores:
                continue
            backups = [
                b
                for stmt in _walk_own_scope(func)
                if getattr(stmt, "lineno", node.lineno) < node.lineno
                and (b := _binds_a_backup(stmt)) is not None
            ]
            if not backups:
                continue
            related = {n for b in backups for n in (b.name, b.source) if n}
            matched = sorted({what for what, names in restores if names & related})
            if matched:
                offenders.append((func.name, node.lineno, ", ".join(matched)))
    return offenders


def _test_files() -> list[Path]:
    return sorted(p for p in _TESTS_ROOT.rglob("*.py") if p.name != Path(__file__).name)


def test_scan_list_is_not_empty() -> None:
    """A broken scan list must not pass vacuously."""
    files = _test_files()
    assert len(files) > 100, f"guard scanned only {len(files)} test files — the sweep is broken"


def test_no_backup_restore_dance_in_tests() -> None:
    offenders: list[str] = []
    for path in _test_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - a broken test file fails elsewhere
            continue
        rel = str(path.relative_to(_TESTS_ROOT))
        for func, lineno, what in backup_restore_dances(tree):
            if (rel, func) in ALLOWED_BACKUP_RESTORE:
                continue
            offenders.append(f"{rel}:{lineno} ({func}) — restores {what} in a finally")

    assert not offenders, (
        "backup-and-restore-in-finally: the file's only copy lives in a local "
        "variable, and a pkill / CI timeout / IDE stop skips the finally — the "
        "copy dies with the process. Do not make the restore more robust; stop "
        "touching the real file. Point HOME at a throwaway dir with "
        "tests/helpers.py:set_home, or use tmp_path, so there is nothing to "
        "restore. This destroyed a real ~/.claude/settings.json — see #1892.\n  "
        + "\n  ".join(offenders)
    )


def test_stale_allowlist_entries_fail() -> None:
    """A stale exemption is a hole — it silently licenses reintroduction."""
    live: set[tuple[str, str]] = set()
    for path in _test_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover
            continue
        rel = str(path.relative_to(_TESTS_ROOT))
        for func, _, _ in backup_restore_dances(tree):
            live.add((rel, func))

    stale = sorted(ALLOWED_BACKUP_RESTORE - live)
    assert not stale, (
        "ALLOWED_BACKUP_RESTORE entries no longer match anything (renamed or "
        "fixed) — remove them:\n  " + "\n  ".join(f"{f} ({fn})" for f, fn in stale)
    )


# -- negative pins: the scanner must actually fire ---------------------------

_CANONICAL_1892 = """
def test_thing():
    target = Path.home() / ".claude" / "settings.json"
    backup = target.read_text(encoding="utf-8") if target.is_file() else None
    try:
        target.write_text("{}", encoding="utf-8")
    finally:
        if backup is not None:
            target.write_text(backup, encoding="utf-8")
        elif target.is_file():
            target.unlink()
"""

_TO_THREAD = """
async def test_thing():
    backup = target.read_text()
    try:
        pass
    finally:
        await asyncio.to_thread(target.write_text, backup)
"""

_SHUTIL_STATEMENT = """
def test_thing():
    shutil.copy2(target, spare)
    try:
        pass
    finally:
        shutil.copy2(spare, target)
"""

_OS_REPLACE = """
def test_thing():
    backup = target.read_bytes()
    try:
        pass
    finally:
        os.replace(spare, target)
"""

_BENIGN_TMP_WRITE = """
def test_thing(tmp_path):
    target = tmp_path / "a.json"
    try:
        target.write_text("{}")
    finally:
        pass
"""

_BENIGN_CLOSE = """
def test_thing():
    f = open("x")
    try:
        f.read()
    finally:
        f.close()
"""

_BENIGN_UNLINK_CLEANUP = """
def test_thing(tmp_path):
    tmp = tmp_path / "scratch"
    try:
        tmp.write_text("x")
    finally:
        tmp.unlink()
"""


@pytest.mark.parametrize(
    "source, expected",
    [
        pytest.param(_CANONICAL_1892, 1, id="canonical-1892-shape"),
        pytest.param(_TO_THREAD, 1, id="to-thread-attribute-not-call"),
        pytest.param(_SHUTIL_STATEMENT, 1, id="shutil-copy-statement-form"),
        pytest.param(_OS_REPLACE, 1, id="os-replace"),
        pytest.param(_BENIGN_TMP_WRITE, 0, id="benign-tmp-write"),
        pytest.param(_BENIGN_CLOSE, 0, id="benign-file-close"),
        pytest.param(_BENIGN_UNLINK_CLEANUP, 0, id="benign-unlink-cleanup-no-backup"),
    ],
)
def test_scanner_discriminates(source: str, expected: int) -> None:
    assert len(backup_restore_dances(ast.parse(source))) == expected


_ANNOTATED_BACKUP = """
def test_thing():
    backup: str = target.read_text(encoding="utf-8")
    try:
        pass
    finally:
        target.write_text(backup)
"""

_AWAITED_BACKUP = """
async def test_thing():
    backup = await asyncio.to_thread(target.read_text)
    try:
        pass
    finally:
        await asyncio.to_thread(target.write_text, backup)
"""

_NESTED_SCOPE = """
def test_thing():
    backup = target.read_text()

    def helper():
        try:
            pass
        finally:
            scratch.unlink()

    helper()
"""

_UNRELATED_CLEANUP = """
def test_thing(tmp_path):
    expected = golden.read_text()
    scratch = tmp_path / "scratch"
    try:
        assert run() == expected
    finally:
        scratch.unlink()
"""


@pytest.mark.parametrize(
    "source, expected",
    [
        pytest.param(_ANNOTATED_BACKUP, 1, id="annotated-assignment-backup"),
        pytest.param(_AWAITED_BACKUP, 1, id="awaited-offloaded-backup"),
        pytest.param(_NESTED_SCOPE, 0, id="nested-scope-is-not-the-parents-dance"),
        pytest.param(_UNRELATED_CLEANUP, 0, id="unrelated-read-plus-unrelated-cleanup"),
    ],
)
def test_scanner_precision(source: str, expected: int) -> None:
    """Shapes Codex found on review of #1902 — three misses and one false positive."""
    assert len(backup_restore_dances(ast.parse(source))) == expected
