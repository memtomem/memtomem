"""Guard: a subprocess env that overrides ``HOME`` must override ``USERPROFILE`` too.

``Path.home()`` reads ``USERPROFILE`` **first** on Windows (then
``HOMEDRIVE``+``HOMEPATH``); ``HOME`` is consulted only on POSIX. So a test that
builds a subprocess environment with ``env["HOME"] = str(sandbox)`` and stops
there is fully sandboxed on Linux/macOS and **not sandboxed at all** on the
``windows-test-shard`` CI jobs — the child process resolves the runner's real
home and writes there.

``tests/helpers.py:set_home`` already encodes this for in-process tests
(``monkeypatch``). Subprocess tests build an ``env`` dict by hand and cannot use
it, so the same rule is pinned here instead.

This is the subprocess half of the #1892 family: a test writing a real home
because its sandbox silently did not apply.

Matcher strictness — deliberately narrow, so a pairing has to be real:

* The ``USERPROFILE`` assignment must target the **same mapping name**. A
  function that sets ``env["HOME"]`` and ``other["USERPROFILE"]`` has paired
  nothing, and a laxer "USERPROFILE appears somewhere in this function" check
  would report it as safe.
* The pairing must be **unconditional** — at the same statement-list depth as the
  ``HOME`` assignment, not nested inside an ``if``. A pairing that only executes
  under ``if sys.platform == "win32":`` is exactly backwards: it is dead on the
  platform that already works and the branch may not be taken on the one that
  does not.

Pattern lineage: ``feedback_ast_architectural_guard_pattern.md``. Registry idiom
mirrors ``test_context_atomic_write_guard.py`` (empty allowlist — every entry
would be a real Windows hole).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_TESTS_ROOT = Path(__file__).resolve().parent

#: ``(path relative to tests/, enclosing function)`` pairs allowed to set ``HOME``
#: without ``USERPROFILE``. Empty by design: an unpaired override is a real
#: Windows sandbox hole, not a style preference. Add an entry ONLY with an inline
#: why (e.g. a test that asserts on the *absence* of the variable), mirroring the
#: DEFERRED registry convention in
#: ``test_validate_namespace_architectural_guard.py``.
ALLOWED_UNPAIRED_HOME: frozenset[tuple[str, str]] = frozenset()


def _mapping_key(node: ast.AST) -> tuple[str, str] | None:
    """Return ``(mapping_name, key)`` for an ``env["HOME"]``-shaped target."""
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
        if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
            return node.value.id, node.slice.value
    return None


def _same_value(home_value: ast.expr, other: ast.expr, mapping: str) -> bool:
    """True if ``other`` assigns the *same* home to ``USERPROFILE``.

    Presence alone is not a pairing: ``env["USERPROFILE"] = str(other_dir)``
    sandboxes the child somewhere the test is not asserting about, which is a
    subtler version of not sandboxing it at all. Accepts the idiomatic
    ``env["USERPROFILE"] = env["HOME"]`` alias as well as a literal repeat of the
    same expression.
    """
    if ast.dump(home_value) == ast.dump(other):
        return True
    key = _mapping_key(other)
    return key == (mapping, "HOME")


def _assignments(body: list[ast.stmt]) -> list[tuple[str, str, int, ast.expr]]:
    """``(mapping, key, lineno, value)`` for subscript assignments directly in ``body``.

    Deliberately does NOT recurse: a sibling assignment must live at the same
    statement-list depth to count as an unconditional pairing.
    """
    found: list[tuple[str, str, int, ast.expr]] = []
    for stmt in body:
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                key = _mapping_key(target)
                if key is not None:
                    found.append((key[0], key[1], stmt.lineno, stmt.value))
    return found


def _iter_statement_lists(node: ast.AST):
    """Yield every statement list in the tree, so each block is checked in isolation."""
    for parent in ast.walk(node):
        for field in ("body", "orelse", "finalbody"):
            block = getattr(parent, field, None)
            if isinstance(block, list) and block and isinstance(block[0], ast.stmt):
                yield block


def _dict_entries(node: ast.Dict) -> dict[str, ast.expr]:
    entries: dict[str, ast.expr] = {}
    for key, value in zip(node.keys, node.values):
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            entries[key.value] = value
    return entries


def _enclosing_function(tree: ast.AST, lineno: int) -> str:
    """Innermost function whose line *range* contains ``lineno``.

    Containment, not "latest ``def`` that starts before this line": with the
    latter, an outer-function statement that follows a nested ``def`` is
    attributed to the nested one, which would make an allowlist entry or a
    stale-entry check point at the wrong name. Same logic as
    ``test_context_atomic_write_guard.py``.
    """
    best: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        end = getattr(node, "end_lineno", None) or node.lineno
        if node.lineno <= lineno <= end:
            if best is None or node.lineno > best.lineno:
                best = node
    return best.name if best is not None else "<module>"


def unpaired_home_overrides(tree: ast.AST) -> list[tuple[str, int, str]]:
    """``(mapping, lineno, function)`` for each unpaired ``HOME`` override.

    Covers both ways a subprocess environment is built in this suite:

    * ``env["HOME"] = …`` — an unconditional sibling assignment on the same
      mapping, at the same statement-list depth, carrying the same value.
    * ``env.update({"HOME": …})`` / ``env = {"HOME": …}`` / ``run(env={...})``
      — a ``USERPROFILE`` entry in the *same dict literal*. Scanning dict
      literals wherever they appear covers ``update()``, direct construction and
      an inline ``env=`` argument without having to special-case each call shape.
    """
    offenders: list[tuple[str, int, str]] = []

    for block in _iter_statement_lists(tree):
        assignments = _assignments(block)
        for mapping, key, lineno, value in assignments:
            if key != "HOME":
                continue
            paired = any(
                m == mapping and k == "USERPROFILE" and _same_value(value, v, mapping)
                for m, k, _, v in assignments
            )
            if not paired:
                offenders.append((mapping, lineno, _enclosing_function(tree, lineno)))

    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        entries = _dict_entries(node)
        if "HOME" not in entries:
            continue
        other = entries.get("USERPROFILE")
        if other is not None and _same_value(entries["HOME"], other, ""):
            continue
        offenders.append(("<dict>", node.lineno, _enclosing_function(tree, node.lineno)))

    return sorted(offenders, key=lambda o: (o[1], o[0]))


def _test_files() -> list[Path]:
    return sorted(p for p in _TESTS_ROOT.rglob("*.py") if p.name != Path(__file__).name)


def test_scan_list_is_not_empty() -> None:
    """A broken scan list must not pass vacuously."""
    files = _test_files()
    assert len(files) > 100, f"guard scanned only {len(files)} test files — the sweep is broken"


def test_every_subprocess_home_override_pairs_userprofile() -> None:
    offenders: list[str] = []
    for path in _test_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - a broken test file fails elsewhere
            continue
        rel = str(path.relative_to(_TESTS_ROOT))
        for mapping, lineno, func in unpaired_home_overrides(tree):
            if (rel, func) in ALLOWED_UNPAIRED_HOME:
                continue
            offenders.append(
                f"{rel}:{lineno} ({func}) — {mapping}['HOME'] with no {mapping}['USERPROFILE']"
            )

    assert not offenders, (
        "subprocess env overrides HOME but not USERPROFILE — the child process is "
        "sandboxed on POSIX and reads the runner's REAL home on Windows, where "
        "Path.home() consults USERPROFILE first. Add "
        "`env['USERPROFILE'] = env['HOME']` beside each assignment (unconditionally, "
        "on the same mapping). In-process tests should use tests/helpers.py:set_home "
        "instead, which sets both. See #1892.\n  " + "\n  ".join(offenders)
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
        for _, _, func in unpaired_home_overrides(tree):
            live.add((rel, func))

    stale = sorted(ALLOWED_UNPAIRED_HOME - live)
    assert not stale, (
        "ALLOWED_UNPAIRED_HOME entries no longer match anything (renamed or fixed) — "
        "remove them:\n  " + "\n  ".join(f"{f} ({fn})" for f, fn in stale)
    )


# -- negative pins: the scanner must actually fire ---------------------------


@pytest.mark.parametrize(
    "source, expected",
    [
        pytest.param('def f():\n    env["HOME"] = h\n', 1, id="bare-home"),
        pytest.param(
            'def f():\n    env["HOME"] = h\n    other["USERPROFILE"] = h\n',
            1,
            id="different-mapping-is-not-a-pairing",
        ),
        pytest.param(
            'def f():\n    env["HOME"] = h\n'
            '    if sys.platform == "win32":\n        env["USERPROFILE"] = h\n',
            1,
            id="conditional-only-is-not-a-pairing",
        ),
        pytest.param(
            'def f():\n    env1["HOME"] = h\n    env1["USERPROFILE"] = h\n    env2["HOME"] = h\n',
            1,
            id="second-mapping-unpaired",
        ),
        pytest.param(
            'def f():\n    env["HOME"] = h\n    env["USERPROFILE"] = h\n',
            0,
            id="paired-is-clean",
        ),
        pytest.param(
            'def f():\n    env["HOME"] = str(home)\n    env["USERPROFILE"] = env["HOME"]\n',
            0,
            id="alias-form-is-a-pairing",
        ),
        pytest.param(
            'def f():\n    env["HOME"] = str(home)\n    env["USERPROFILE"] = str(other)\n',
            1,
            id="different-value-is-not-a-pairing",
        ),
        pytest.param('def f():\n    env["XDG_RUNTIME_DIR"] = x\n', 0, id="unrelated-key-is-clean"),
        # The dict-literal form. A subscript-only matcher missed a real site
        # (web/test_actual_lifespan_golden.py builds its env via env.update).
        pytest.param(
            'def f():\n    env.update({"HOME": str(home), "TMPDIR": t})\n',
            1,
            id="update-dict-without-userprofile",
        ),
        pytest.param(
            'def f():\n    env.update({"HOME": str(home), "USERPROFILE": str(home)})\n',
            0,
            id="update-dict-paired",
        ),
        pytest.param(
            'def f():\n    env = {"HOME": str(home)}\n',
            1,
            id="dict-construction-without-userprofile",
        ),
        pytest.param(
            'def f():\n    run(cmd, env={"HOME": h, "USERPROFILE": h})\n',
            0,
            id="inline-env-argument-paired",
        ),
    ],
)
def test_scanner_discriminates(source: str, expected: int) -> None:
    assert len(unpaired_home_overrides(ast.parse(source))) == expected


def test_attribution_uses_containment_not_latest_def() -> None:
    """An outer-function statement after a nested ``def`` belongs to the outer one.

    "Latest ``def`` starting before this line" would name ``inner`` here, which
    would make an allowlist entry — or the stale-entry check — point at a
    function that does not contain the offending line.
    """
    source = "def outer():\n    def inner():\n        pass\n\n    env['HOME'] = h\n"
    offenders = unpaired_home_overrides(ast.parse(source))
    assert [o[2] for o in offenders] == ["outer"]
