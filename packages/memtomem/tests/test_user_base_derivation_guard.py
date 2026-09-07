"""Architectural guard: the user-tier base is derived in exactly one place.

``memory_dirs[0]`` is the destination of every write that takes no scope from
its caller — the session-end summary archive, the Notion / Obsidian importers,
``mem_fetch``, scratch promote, ``mm review approve``, ``mm agent share``,
``mm shell``'s ``add``. #2322 is what happens when that expression is spelled
out at the call site instead of asked for: ``memory_dirs`` and
``project_memory_dirs`` may overlap, and a base that is a registered project
tier puts an *unconfirmed* write into the git-tracked tier — the exact
ADR-0011 §5 Gate B hole the issue reports.

:func:`memtomem.memory_scope.require_user_base` now refuses that base. The
refusal is only worth something if every derivation goes through it, so this
file AST-scans ``src/memtomem`` for the hand-rolled shape and fails on any
site that is not classified in :data:`ALLOWED`.

Three spellings count. The direct attribute form::

    base = Path(comp.config.indexing.memory_dirs[0]).expanduser().resolve()

the one that goes through a local of the same name, which is how
``session.py`` escaped a repo-wide grep for
``config.indexing.memory_dirs[0]``::

    memory_dirs = app.config.indexing.memory_dirs
    base = Path(memory_dirs[0]).expanduser().resolve()

and the same thing under any *other* local name, which is one rename away in
the tree as it stands — ``server/tools/memory_crud.py`` really does write
``mdirs = app.config.indexing.memory_dirs``::

    mdirs = app.config.indexing.memory_dirs
    base = Path(mdirs[0]).expanduser().resolve()

The third is why the scan tracks assignment targets whose value is a
``.memory_dirs`` attribute, per function, rather than matching a fixed name.

What this guard does **not** claim, stated because a guard that overstates
its reach is worse than one that does not exist. It matches by name and by
literal index ``0``. These evade it:

* a helper that returns the list (``get_dirs()[0]``) — the alias chain is
  one assignment deep, not interprocedural;
* first-element extraction that is not a subscript — ``next(iter(dirs))``,
  ``first, *rest = dirs``, ``dirs.pop(0)``;
* an index that is a variable rather than the literal ``0``.

Widening to any of those means flagging shapes that are usually innocent, so
the line is drawn at the ones actually seen in this tree. The
:data:`ALLOWED` registry is count-sensitive for the same reason a name is not
enough: an allowlisted *function* would otherwise hide a second derivation
added inside it later.

It also says nothing about whether a site that *does* call
``require_user_base`` passes the real ``project_memory_dirs`` — that is the
helper's own required parameter and the unit pins in
``test_require_user_base_tier.py``.

Pattern lineage: ``test_project_shared_confirmation_audit_guard.py``
(unclassified-fails registry + detector self-tests),
``feedback_ast_architectural_guard_pattern.md``.
"""

from __future__ import annotations

import ast
import pathlib
import textwrap

import pytest

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "memtomem"

_FIELD = "memory_dirs"

# ``(path relative to src/memtomem, function, how many derivations, why)`` —
# sites that may index the list directly. A bare path is not an entry: the
# reason is what a future reader needs in order to decide whether a new entry
# belongs here, and the **count** is what stops an allowlisted function from
# becoming a place to park a second one.
ALLOWED: frozenset[tuple[str, str, int, str]] = frozenset(
    {
        (
            "memory_scope.py",
            "require_user_base",
            1,
            "The canonical derivation itself — this is the site that classifies "
            "the base and refuses a project tier (#2322).",
        ),
        (
            "cli/shell.py",
            "_cmd_index",
            1,
            "Reads, does not write: `index` with no argument defaults to "
            "indexing the first memory dir. Indexing a project tier is "
            "legitimate; only writing into one without Gate B is not.",
        ),
        (
            "pinned.py",
            "__init__",
            1,
            "Deliberate read-side fallback. PinnedContextStore must construct "
            "on any loadable config (#1768: mem_context_compose answers with a "
            "bundle, not an internal error), and search_exclusion_roots / the "
            "shadowing logic need the user-tier path even when it is refused "
            "for writes. The constructor calls require_user_base alongside "
            "this line purely to capture the refusal, and PinnedContextStore."
            "set raises it (#2322).",
        ),
    }
)

#: ``{(relpath, function): expected count}`` — the membership + budget view.
_ALLOWED_COUNTS: dict[tuple[str, str], int] = {(rel, fn): n for rel, fn, n, _ in ALLOWED}

_FUNC_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)


def _src_files() -> list[pathlib.Path]:
    return sorted(_SRC.rglob("*.py"))


def _rel(path: pathlib.Path) -> str:
    """Registry key for *path*: always ``a/b.py``, never ``a\\b.py``.

    ``str()`` on a Windows path gives backslashes, which would miss every
    lookup and fail the whole registry at once. Normalised in one place.
    """
    return path.relative_to(_SRC).as_posix()


def _alias_names(tree: ast.AST) -> set[str]:
    """Locals bound to a ``.memory_dirs`` attribute anywhere in *tree*.

    One assignment deep and scope-insensitive on purpose. Deeper chains and
    cross-function flow are the interprocedural analysis this guard says it
    does not do; a module-wide name set only ever makes the scan *stricter*,
    which for a guard is the safe direction — a false positive is one
    allowlist line, a false negative is the bug shipping.
    """
    names = {_FIELD}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Attribute):
            if node.value.attr == _FIELD:
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.value, ast.Attribute):
            if node.value.attr == _FIELD and isinstance(node.target, ast.Name):
                names.add(node.target.id)
    return names


def _names_the_field(node: ast.expr, aliases: set[str]) -> bool:
    """Is *node* ``<...>.memory_dirs`` or a local standing in for it?"""
    if isinstance(node, ast.Attribute):
        return node.attr == _FIELD
    return isinstance(node, ast.Name) and node.id in aliases


def _is_index_zero(node: ast.Subscript) -> bool:
    """``[0]`` specifically — not ``[1]``, not ``[i]``.

    Only the first entry is the user-tier base; the guard's claim is about
    that one expression, and saying so keeps it from flagging unrelated
    indexing.
    """
    sl = node.slice
    return isinstance(sl, ast.Constant) and sl.value == 0


def _enclosing_function(tree: ast.AST, lineno: int) -> str:
    """Innermost function containing *lineno* (for the report and registry)."""
    best = "<module>"
    best_line = -1
    for node in ast.walk(tree):
        if isinstance(node, _FUNC_NODES):
            end = getattr(node, "end_lineno", node.lineno)
            if node.lineno <= lineno <= end and node.lineno > best_line:
                best, best_line = node.name, node.lineno
    return best


def _sites(tree: ast.AST, rel: str) -> list[tuple[str, str, int]]:
    """Every ``memory_dirs[0]`` derivation in one module, classified or not."""
    aliases = _alias_names(tree)
    found: list[tuple[str, str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and _names_the_field(node.value, aliases):
            if _is_index_zero(node):
                found.append((rel, _enclosing_function(tree, node.lineno), node.lineno))
    return found


def _offenses(
    tree: ast.AST,
    rel: str,
    *,
    allowed: dict[tuple[str, str], int] | None = None,
) -> list[str]:
    """Unclassified derivations, plus allowlisted ones that grew a second.

    The count is the second half of the classification. Keying only on
    ``(file, function)`` would make an allowlisted function a place to hide
    a new derivation — the entry was written about *one* expression, and it
    should stop describing the function the moment there are two.
    """
    budget = _ALLOWED_COUNTS if allowed is None else allowed
    sites = _sites(tree, rel)
    offenses = [
        f"{rel}:{line} ({fn}) derives the user-tier base by hand"
        for rel, fn, line in sites
        if (rel, fn) not in budget
    ]
    # Only this module's budget rows: ``_offenses`` runs per file, so a row
    # for another file is not this file's business.
    counted: dict[tuple[str, str], list[int]] = {key: [] for key in budget if key[0] == rel}
    for _rel, fn, line in sites:
        if (rel, fn) in counted:
            counted[(rel, fn)].append(line)
    for key, lines in counted.items():
        expected = budget[key]
        if len(lines) != expected:
            offenses.append(
                f"{key[0]} ({key[1]}) has {len(lines)} derivations at lines "
                f"{sorted(lines)}, but its ALLOWED entry budgets {expected}"
            )
    return offenses


def _snippet_sites(source: str) -> list[tuple[str, str, int]]:
    return _sites(ast.parse(textwrap.dedent(source)), "snippet.py")


def _snippet_offenses(source: str, allowed: dict[tuple[str, str], int] | None = None) -> list[str]:
    return _offenses(ast.parse(textwrap.dedent(source)), "snippet.py", allowed=allowed or {})


# ── the scan ──────────────────────────────────────────────────────────────


def test_no_hand_rolled_user_base_derivation() -> None:
    offenses: list[str] = []
    for path in _src_files():
        offenses.extend(_offenses(ast.parse(path.read_text(encoding="utf-8")), _rel(path)))
    assert not offenses, (
        "The user-tier base must come from memory_scope.require_user_base("
        "memory_dirs, project_memory_dirs), which refuses a base that is a "
        "registered project tier (#2322). Indexing the list here skips that "
        "refusal.\n  " + "\n  ".join(offenses)
    )


def test_the_scan_actually_reaches_the_allowlisted_sites() -> None:
    """Witness: the scan finds the sites it is meant to be classifying.

    Without this, a broken matcher would make ``test_no_hand_rolled_...``
    pass by finding nothing at all — a guard reporting silence and a guard
    reporting a clean tree look identical from the assertion.
    """
    seen: dict[tuple[str, str], int] = {}
    for path in _src_files():
        for rel, fn, _line in _sites(ast.parse(path.read_text(encoding="utf-8")), _rel(path)):
            seen[(rel, fn)] = seen.get((rel, fn), 0) + 1
    assert seen == _ALLOWED_COUNTS, (
        "The hand-rolled derivations in the tree no longer match the registry. "
        "Extra keys are unclassified sites; missing keys are stale registry "
        "rows; a differing count means an allowlisted function grew or lost "
        f"one.\n  found: {sorted(seen.items())}\n  registry: {sorted(_ALLOWED_COUNTS.items())}"
    )


def test_allowlist_entries_carry_a_reason_and_a_positive_count() -> None:
    for rel, fn, count, why in ALLOWED:
        assert why.strip(), f"{rel}:{fn} has no reason"
        assert count >= 1, f"{rel}:{fn} budgets {count} derivations; an entry describes at least 1"


# ── detector self-tests ───────────────────────────────────────────────────


def test_detector_flags_the_attribute_form() -> None:
    offenses = _snippet_offenses("""
        def f(comp):
            base = Path(comp.config.indexing.memory_dirs[0]).expanduser().resolve()
            return base
    """)
    assert len(offenses) == 1
    assert "(f)" in offenses[0]


def test_detector_flags_the_local_alias_form() -> None:
    """The shape that let ``session.py`` escape a grep for the attribute."""
    offenses = _snippet_offenses("""
        def f(app):
            memory_dirs = app.config.indexing.memory_dirs
            if not memory_dirs:
                return None
            base = Path(memory_dirs[0]).expanduser().resolve()
            return base
    """)
    assert len(offenses) == 1
    assert "(f)" in offenses[0]


def test_detector_accepts_the_helper_call() -> None:
    assert (
        _snippet_offenses("""
        def f(comp):
            return require_user_base(
                comp.config.indexing.memory_dirs, comp.config.indexing.project_memory_dirs
            )
    """)
        == []
    )


@pytest.mark.parametrize(
    "index",
    ["1", "i", "-1", "idx + 1"],
    ids=["second", "variable", "negative", "expression"],
)
def test_detector_only_claims_index_zero(index: str) -> None:
    """The guard's claim is about *the user-tier base*, which is entry 0.

    Widening it to any subscript would flag unrelated indexing and make the
    registry meaningless.
    """
    assert (
        _snippet_offenses(f"""
        def f(comp):
            return comp.config.indexing.memory_dirs[{index}]
    """)
        == []
    )


def test_detector_ignores_iteration_over_the_list() -> None:
    assert (
        _snippet_offenses("""
        def f(config):
            return [Path(d).expanduser().resolve() for d in config.indexing.memory_dirs]
    """)
        == []
    )


def test_detector_ignores_a_similarly_named_field() -> None:
    assert (
        _snippet_offenses("""
        def f(config):
            return config.indexing.project_memory_dirs[0]
    """)
        == []
    )


def test_detector_flags_an_alias_under_another_name() -> None:
    """``mdirs = app.config.indexing.memory_dirs`` is real code in
    ``server/tools/memory_crud.py``; ``mdirs[0]`` is one line away."""
    offenses = _snippet_offenses("""
        def f(app):
            mdirs = app.config.indexing.memory_dirs
            base = Path(mdirs[0]).expanduser().resolve()
            return base
    """)
    assert len(offenses) == 1
    assert "(f)" in offenses[0]


def test_detector_flags_an_annotated_alias() -> None:
    offenses = _snippet_offenses("""
        def f(app):
            dirs: list[Path] = app.config.indexing.memory_dirs
            return dirs[0]
    """)
    assert len(offenses) == 1


def test_detector_ignores_an_unrelated_local_named_like_an_alias() -> None:
    """The alias set is built from assignments, not from plausible names —
    a list that never came from ``.memory_dirs`` is not a derivation."""
    assert (
        _snippet_offenses("""
        def f(paths):
            mdirs = [p for p in paths]
            return mdirs[0]
    """)
        == []
    )


def test_detector_honours_an_allowlist_entry() -> None:
    source = """
        def f(comp):
            return comp.config.indexing.memory_dirs[0]
    """
    assert len(_snippet_offenses(source)) == 1
    assert _snippet_offenses(source, allowed={("snippet.py", "f"): 1}) == []


def test_an_allowlisted_function_cannot_hide_a_second_derivation() -> None:
    """The entry was written about one expression. A function that grows a
    second one is no longer the thing the reason describes, so the budget —
    not just the name — has to be re-read by a human."""
    source = """
        def f(comp):
            first = comp.config.indexing.memory_dirs[0]
            second = comp.config.indexing.memory_dirs[0]
            return first, second
    """
    offenses = _snippet_offenses(source, allowed={("snippet.py", "f"): 1})
    assert len(offenses) == 1
    assert "has 2 derivations" in offenses[0]
    assert "budgets 1" in offenses[0]


def test_an_allowlisted_function_that_lost_its_derivation_is_stale() -> None:
    """The budget bites in both directions: an entry describing a site that
    no longer exists is a stale row, not a silent pass."""
    offenses = _snippet_offenses(
        """
        def f(comp):
            return require_user_base(
                comp.config.indexing.memory_dirs, comp.config.indexing.project_memory_dirs
            )
    """,
        allowed={("snippet.py", "f"): 1},
    )
    assert len(offenses) == 1
    assert "has 0 derivations" in offenses[0]


def test_detector_reports_the_enclosing_function_not_the_module() -> None:
    """The registry key is ``(file, function)``; a site attributed to
    ``<module>`` would be unclassifiable."""
    sites = _snippet_sites("""
        def outer(comp):
            def inner():
                return comp.config.indexing.memory_dirs[0]
            return inner
    """)
    assert [fn for _rel, fn, _line in sites] == ["inner"]
