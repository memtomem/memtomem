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


def _binds_a_backup(node: ast.AST) -> bool:
    """True if ``node`` binds a file's contents (or a copy of it) to a name.

    Two shapes, both seen in the wild:

    * ``backup = target.read_text(...)`` / ``.read_bytes(...)`` — including the
      ``... if target.is_file() else None`` conditional form used in #1892, since
      the read is still an ``ast.Call`` inside the assigned value.
    * ``shutil.copy2(target, backup_path)`` — statement form, no assignment.
    """
    if isinstance(node, ast.Assign):
        for sub in ast.walk(node.value):
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute):
                if sub.func.attr in _BACKUP_READS:
                    return True
        return False
    if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
        call = node.value
        if isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Name):
            if (call.func.value.id, call.func.attr) in _RESTORE_FUNCS:
                # A copy *to* a plain name reads as "stash a spare copy here".
                return any(isinstance(arg, ast.Name) for arg in call.args)
    return False


def _restores_in_finally(finalbody: list[ast.stmt]) -> list[str]:
    """Restore-shaped operations appearing anywhere in a ``finally`` block."""
    found: list[str] = []
    for stmt in finalbody:
        for node in ast.walk(stmt):
            # ATTRIBUTE references, not just calls: the repo has already been
            # bitten by ``asyncio.to_thread(path.write_text, data)``, where
            # ``write_text`` is passed as a value and never appears as the func
            # of a Call. See test_context_atomic_write_guard.py.
            if isinstance(node, ast.Attribute) and node.attr in _RESTORE_ATTRS:
                if isinstance(node.value, ast.Name):
                    found.append(f"{node.value.id}.{node.attr}")
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                func = node.func
                if isinstance(func.value, ast.Name):
                    if (func.value.id, func.attr) in _RESTORE_FUNCS:
                        if any(isinstance(arg, ast.Name) for arg in node.args):
                            found.append(f"{func.value.id}.{func.attr}")
    return found


def backup_restore_dances(tree: ast.AST) -> list[tuple[str, int, str]]:
    """``(function, lineno, what)`` for each backup-then-restore-in-finally shape.

    A ``finally`` that restores is only reported when the enclosing function also
    binds a backup *before* the ``try``. Without that pairing a bare
    ``tmp.unlink()`` in a ``finally`` — ordinary temp cleanup — would be flagged,
    which would make the guard noise rather than signal.
    """
    offenders: list[tuple[str, int, str]] = []
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(func):
            if not isinstance(node, ast.Try) or not node.finalbody:
                continue
            restores = _restores_in_finally(node.finalbody)
            if not restores:
                continue
            has_backup = any(
                _binds_a_backup(stmt)
                for stmt in ast.walk(func)
                if getattr(stmt, "lineno", node.lineno) < node.lineno
            )
            if has_backup:
                offenders.append((func.name, node.lineno, ", ".join(sorted(set(restores)))))
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
