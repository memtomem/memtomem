"""Architectural guard for ``validate_namespace`` coverage on
``server/tools/*.py``.

Closes the regression class the multi-agent namespace-gate series
(#491 → #494 → #496 → #498 → #499 → #500/#501) kept hitting one PR at
a time: a public MCP tool gets added or refactored that takes a
namespace-shaped argument, but the new code path forgets to import /
call :func:`validate_namespace`. The series itself is the evidence —
every PR closed one more surface that had been silently ungated.

This file makes the contract **declarative** rather than per-tool:

* :data:`VALIDATED_NS_SURFACES` — every ``(file, function, parameter)``
  triple where ``validate_namespace`` MUST appear in the function body.
  A regression that drops the call (refactor, accidental deletion, copy-
  paste from a deferred surface) trips :func:`test_declared_validated_
  surfaces_call_validate_namespace`.
* :data:`DEFERRED_NS_SURFACES` — every triple the project has
  *explicitly* decided to leave ungated for now (broader UX call deferred
  in issue #500). Each entry carries the rationale inline so a future
  reader can look up *why* the deferral is in place rather than wonder
  whether it's an oversight.
* :data:`WRITABLE_NS_SURFACES` — write surfaces that must rebind their
  argument through ``writable_namespace_arg`` (#2933), the narrower gate
  for names that land in chunk rows.
* :data:`DELEGATED_NS_SURFACES` — functions that only forward a value
  their callers already gated.
* :func:`test_no_unclassified_ns_surfaces` — AST-scans
  ``server/tools/*.py`` for any tool that takes a namespace-shaped
  parameter. Every match must be classified into either set; an
  unclassified hit fails the test, forcing the author to make a decision
  rather than ship a silent gap. Every match must be classified into one
  of the four sets.

The result: the next time someone adds ``mem_foo(namespace=...)``
without calling ``validate_namespace``, this test fails at PR time and
points them at the missing decision — gate it (move to VALIDATED) or
explicitly defer it (move to DEFERRED with rationale).
"""

from __future__ import annotations

import ast
import pathlib

import pytest

# ---------------------------------------------------------------------------
# Surface registry
# ---------------------------------------------------------------------------

# Triples are ``(filename within server/tools, function_name, parameter)``.
# Multiple parameters on the same function get one triple each.

# Surfaces that MUST call ``validate_namespace`` on the named parameter
# before any storage / state mutation. These are the public MCP tools
# whose namespace argument lands in either ``app.current_namespace``,
# the ``sessions`` row, or chunks/namespace-metadata rows.
VALIDATED_NS_SURFACES: frozenset[tuple[str, str, str]] = frozenset(
    {
        # PR #499 — the original namespace-override gate.
        ("session.py", "mem_session_start", "namespace"),
        ("multi_agent.py", "mem_agent_share", "target"),
        # ADR-0028 — per-project shared bucket override on the agent read path.
        ("multi_agent.py", "mem_agent_search", "shared_namespace"),
        # PR #501 (issue #500) — namespace CRUD gate.
        ("namespace.py", "mem_ns_set", "namespace"),
        ("namespace.py", "mem_ns_delete", "namespace"),
        ("namespace.py", "mem_ns_rename", "old"),
        ("namespace.py", "mem_ns_rename", "new"),
        ("namespace.py", "mem_ns_assign", "namespace"),
        ("namespace.py", "mem_ns_assign", "old_namespace"),
        ("namespace.py", "mem_ns_update", "namespace"),
    }
)

# Write surfaces whose caller-supplied ``namespace`` lands in chunk rows
# (#2933). Each must rebind the argument through ``writable_namespace_arg``
# before anything is written: ``<param> = writable_namespace_arg(<param>)``.
# The gate is narrower than ``validate_namespace`` on purpose -- namespace
# rules store shapes the strict gate refuses (``claude:-Users-...``), and a
# write into a namespace the store already uses must keep working -- so it
# refuses what the read side cannot address by exact name: a comma list, a
# glob, the ``!system`` selector, a blank, a malformed ``agent-runtime:`` id.
# The web add/index routes, ``mm index --namespace`` and the LangGraph
# ``MemtomemStore.add``/``.index`` call the same helper; their behaviour is
# pinned in ``test_writable_namespace_gate.py``, since this guard scans
# ``server/tools`` only.
WRITABLE_NS_SURFACES: frozenset[tuple[str, str, str]] = frozenset(
    {
        ("export_import.py", "mem_import", "namespace"),
        ("importers.py", "mem_import_notion", "namespace"),
        ("importers.py", "mem_import_obsidian", "namespace"),
        ("indexing.py", "mem_index", "namespace"),
        ("memory_crud.py", "mem_add", "namespace"),
        ("memory_crud.py", "mem_batch_add", "namespace"),
        ("url_index.py", "mem_fetch", "namespace"),
    }
)

# Functions that take a namespace-shaped parameter but only forward a value
# their callers already gated. Gating here would double-validate the same
# value; each entry names who gates it.
DELEGATED_NS_SURFACES: frozenset[tuple[str, str, str]] = frozenset(
    {
        # Shared core of ``mem_add``. Its other callers pass ``None``
        # (formation, reflection, scratch), ``mem_agent_share``'s target
        # (validated strictly above), or a namespace consolidation read back
        # from stored rows -- re-gating that would refuse a summary for a
        # legacy namespace the store already holds.
        ("memory_crud.py", "_mem_add_core", "namespace"),
        # Delegates to ``mem_add``, which gates the value.
        ("procedure.py", "mem_procedure_save", "namespace"),
        # Forward the write surfaces' already-gated ``namespace`` and only
        # decide what to fall back to when it is None (#2104 split form
        # reports which of the two sources answered).
        ("_provenance.py", "capture_session_and_namespace", "namespace"),
        ("_provenance.py", "capture_session_and_namespace_split", "namespace"),
    }
)

# Surfaces the project has explicitly left ungated. Each entry carries its
# rationale inline; if that changes, move the triple to the set that gates it
# and add the call to the function body in the same PR.
DEFERRED_NS_SURFACES: frozenset[tuple[str, str, str]] = frozenset(
    {
        # Read-filter surfaces — namespace narrows results, never writes.
        # Hostile shape does not round-trip into storage.
        ("ask.py", "mem_ask", "namespace"),
        ("browse.py", "mem_list", "namespace"),
        ("entity.py", "mem_entity_scan", "namespace"),
        ("entity.py", "mem_entity_search", "namespace"),
        ("evaluation.py", "mem_eval", "namespace"),
        ("export_import.py", "mem_export", "namespace"),
        ("importance.py", "mem_importance_scan", "namespace"),
        ("pinned.py", "mem_context_compose", "namespace"),
        ("recall.py", "mem_recall", "namespace"),
        ("reflection.py", "mem_reflect", "namespace"),
        ("search.py", "mem_search", "namespace"),
        ("temporal.py", "mem_timeline", "namespace"),
        ("temporal.py", "mem_activity", "namespace"),
        # Select stored rows by namespace rather than naming where new rows
        # go: ``mem_consolidate`` filters the sources it groups, and
        # ``mem_delete`` removes the chunks stored under an exact name, which
        # must keep working for a name no write would accept today.
        ("consolidation.py", "mem_consolidate", "namespace"),
        ("memory_crud.py", "mem_delete", "namespace"),
        # Not a surface of its own (#2195): ``merge_agent_namespace_filter``
        # is a pure string-merge helper that picks which shared bucket the
        # agent's private scope is joined with. Both callers gate the value
        # before handing it over — ``mem_agent_search`` via the
        # ``VALIDATED_NS_SURFACES`` entry above, ``mm agent search`` via the
        # same ``validate_namespace`` call on its ``--shared-namespace``
        # option — so gating here would double-validate the same value while
        # leaving neither caller's own parameter any safer. It moves if a
        # caller that does not validate is ever added.
        ("multi_agent.py", "merge_agent_namespace_filter", "shared_namespace"),
        # ``mem_tag_merge.target`` is a *tag name*, not a namespace — caught
        # by the parameter-name heuristic but semantically distinct from the
        # ``mem_agent_share.target`` shape above. Tags travel through
        # ``services.tag_management`` and SQLite ``chunks.tags`` JSON; no
        # namespace gate applies.
        ("tag_management.py", "mem_tag_merge", "target"),
        # ``target`` here is the *file path* being written to, not a
        # namespace — the parameter-name heuristic catches it, but these two
        # #2005 helpers take a ``Path`` the caller already validated
        # (``_validate_path`` for an explicit ``file=``, or a name this
        # module built itself). Their namespace input is ``effective_ns``,
        # which arrives from the write surfaces classified above.
        ("memory_crud.py", "_day_file_retarget", "target"),
        ("memory_crud.py", "_namespace_mix_refusal", "target"),
    }
)


_ALL_SETS: dict[str, frozenset[tuple[str, str, str]]] = {
    "VALIDATED": VALIDATED_NS_SURFACES,
    "WRITABLE": WRITABLE_NS_SURFACES,
    "DELEGATED": DELEGATED_NS_SURFACES,
    "DEFERRED": DEFERRED_NS_SURFACES,
}


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------


_TOOLS_DIR = pathlib.Path(__file__).resolve().parents[1] / "src" / "memtomem" / "server" / "tools"
_NS_PARAM_NAMES: frozenset[str] = frozenset(
    {"namespace", "target", "old", "new", "old_namespace", "shared_namespace"}
)


def _iter_tool_functions() -> list[tuple[str, ast.AsyncFunctionDef | ast.FunctionDef]]:
    """Return ``(filename, function_node)`` for every function in
    ``server/tools/*.py``. Includes both private and public functions —
    private helpers like ``_mem_add_core`` participate in the gate too
    (they are the shared core ``mem_add`` / ``mem_batch_add`` route
    through, so deferring or validating must cover them).
    """
    out: list[tuple[str, ast.AsyncFunctionDef | ast.FunctionDef]] = []
    for path in sorted(_TOOLS_DIR.glob("*.py")):
        if path.name == "__init__.py":
            continue
        # Pin UTF-8 explicitly — Python 3.15 makes UTF-8 the default for
        # ``read_text`` but py312 still resolves it from the locale, so the
        # guard would otherwise be environment-dependent on a non-UTF-8
        # CI runner. Tool files are ASCII today; this is forward-shielding.
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                out.append((path.name, node))
    return out


def _ns_params(node: ast.AsyncFunctionDef | ast.FunctionDef) -> list[str]:
    """Return parameter names matching ``_NS_PARAM_NAMES``, in declaration
    order. Both positional-or-keyword and keyword-only args are scanned
    so a future tool with a kw-only ``namespace`` is still gated.
    """
    return [a.arg for a in (*node.args.args, *node.args.kwonlyargs) if a.arg in _NS_PARAM_NAMES]


def _calls_validate_namespace_on(node: ast.AsyncFunctionDef | ast.FunctionDef, param: str) -> bool:
    """Return True iff the function body contains a call shaped like
    ``validate_namespace(<param>)`` — the callee is named exactly
    ``validate_namespace`` (bare ``validate_namespace(...)`` or an
    attribute access ``mod.validate_namespace(...)``) and the first
    positional argument is ``Name(id=<param>)``.

    The match is deliberately *strict* on the call form — the project
    convention is ``validate_namespace(<param>)`` written exactly that
    way, and the guard depends on that convention being unambiguous.
    The following intentionally do **not** match; if you need any of
    them, the guard registry in this file should be updated to reflect
    the new convention rather than the matcher relaxed:

    * Keyword form: ``validate_namespace(value=namespace)``
    * Renamed local: ``local = namespace; validate_namespace(local)``
    * Aliased import: ``from memtomem.constants import
      validate_namespace as vn; vn(namespace)``

    What still matches (the patterns the guarded surfaces actually
    use today):

    * Conditional gate: ``if old_namespace is not None:
      validate_namespace(old_namespace)`` — the matcher walks the
      function body, so any nested call site counts. Used by
      ``mem_ns_assign`` for the optional ``old_namespace`` arg.
    * Attribute access on a module: ``constants.validate_namespace(
      namespace)`` — the matcher checks ``func.attr`` for
      ``ast.Attribute`` nodes.
    """
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        func = sub.func
        # Accept either ``validate_namespace(...)`` (Name) or
        # ``mod.validate_namespace(...)`` (Attribute). Aliased imports
        # rebind the Name's id and intentionally fail to match — see
        # the docstring above.
        callee = (
            func.attr
            if isinstance(func, ast.Attribute)
            else func.id
            if isinstance(func, ast.Name)
            else None
        )
        if callee != "validate_namespace":
            continue
        if not sub.args:
            continue
        first = sub.args[0]
        if isinstance(first, ast.Name) and first.id == param:
            return True
    return False


_NESTED_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


def _walk_own_scope(node: ast.AsyncFunctionDef | ast.FunctionDef):
    """Yield the nodes of *node*'s body, not descending into nested scopes."""
    stack: list[ast.AST] = list(node.body)
    while stack:
        current = stack.pop()
        yield current
        if isinstance(current, _NESTED_SCOPES):
            continue
        stack.extend(ast.iter_child_nodes(current))


def _rebinds_through_writable_namespace_arg(
    node: ast.AsyncFunctionDef | ast.FunctionDef, param: str
) -> bool:
    """Return True iff the body contains ``<param> = writable_namespace_arg(<param>)``.

    The rebinding is the contract, not just the call: the helper normalizes
    ``""`` to ``None``, so a bare ``writable_namespace_arg(namespace)`` whose
    result is dropped would leave an empty string flowing on to an indexer
    that stores it as an explicit namespace. Same strictness as
    :func:`_calls_validate_namespace_on` on the call form. Nested function,
    lambda and class bodies are not searched: a rebinding there gates a
    closure's local, not the surface's argument.
    """
    for sub in _walk_own_scope(node):
        if not isinstance(sub, ast.Assign) or len(sub.targets) != 1:
            continue
        target, value = sub.targets[0], sub.value
        if not (isinstance(target, ast.Name) and target.id == param):
            continue
        if not isinstance(value, ast.Call) or not value.args:
            continue
        func = value.func
        callee = (
            func.attr
            if isinstance(func, ast.Attribute)
            else func.id
            if isinstance(func, ast.Name)
            else None
        )
        first = value.args[0]
        if callee == "writable_namespace_arg" and isinstance(first, ast.Name):
            if first.id == param:
                return True
    return False


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "filename, fn_name, param",
    sorted(VALIDATED_NS_SURFACES),
)
def test_declared_validated_surfaces_call_validate_namespace(
    filename: str, fn_name: str, param: str
) -> None:
    """Every entry in :data:`VALIDATED_NS_SURFACES` must call
    ``validate_namespace(<param>)`` in its body. A failure here means
    one of the gates dropped the validator call — the regression class
    this file exists to catch.
    """
    matches = [
        node for fname, node in _iter_tool_functions() if fname == filename and node.name == fn_name
    ]
    assert matches, (
        f"Declared validated surface not found in source: "
        f"{filename}::{fn_name}. Either the function was renamed / "
        f"removed, or VALIDATED_NS_SURFACES is stale."
    )
    node = matches[0]
    assert _calls_validate_namespace_on(node, param), (
        f"{filename}::{fn_name} declares ``{param}`` as a validated "
        f"namespace surface but the body does not contain "
        f"``validate_namespace({param})``. If the gate was intentionally "
        f"removed (e.g. moving to a deeper layer), update "
        f"VALIDATED_NS_SURFACES; otherwise restore the validator call "
        f"before any storage / state mutation."
    )


@pytest.mark.parametrize("filename, fn_name, param", sorted(WRITABLE_NS_SURFACES))
def test_declared_writable_surfaces_rebind_through_the_gate(
    filename: str, fn_name: str, param: str
) -> None:
    """Every :data:`WRITABLE_NS_SURFACES` entry rebinds its argument through the gate."""
    matches = [
        node for fname, node in _iter_tool_functions() if fname == filename and node.name == fn_name
    ]
    assert matches, f"Declared writable surface not found in source: {filename}::{fn_name}."
    assert _rebinds_through_writable_namespace_arg(matches[0], param), (
        f"{filename}::{fn_name} declares ``{param}`` as a writable namespace "
        f"surface but the body does not contain "
        f"``{param} = writable_namespace_arg({param})``. Restore it before "
        f"anything is written, or move the entry to the set that describes "
        f"what the function does now."
    )


def test_no_unclassified_ns_surfaces() -> None:
    """Every tool function with a namespace-shaped parameter must be
    explicitly classified into either VALIDATED or DEFERRED. An
    unclassified hit fails this test — forcing the author of a new
    namespace-touching tool to make the validation decision instead of
    silently shipping it.

    This is the prospective regression catch the
    ``feedback_drift_close_must_derive`` and ``feedback_stub_gap_check``
    memos warn about: every literal that lists "the gated surfaces"
    eventually drifts unless a guard forces re-classification when the
    code adds new ones.
    """
    declared = _ALL_SETS["VALIDATED"] | _ALL_SETS["WRITABLE"]
    declared |= _ALL_SETS["DELEGATED"] | _ALL_SETS["DEFERRED"]
    found: set[tuple[str, str, str]] = set()
    for filename, node in _iter_tool_functions():
        for p in _ns_params(node):
            found.add((filename, node.name, p))

    unclassified = found - declared
    stale = declared - found

    msg_parts = []
    if unclassified:
        msg_parts.append(
            "Unclassified namespace surfaces — every tool with a "
            "``namespace=``/``target=``/``old=``/``new=``/"
            "``old_namespace=`` parameter must be added to "
            "VALIDATED_NS_SURFACES (strict gate), WRITABLE_NS_SURFACES "
            "(writes chunk rows through writable_namespace_arg), "
            "DELEGATED_NS_SURFACES (forwards a value its callers gated) or "
            "DEFERRED_NS_SURFACES (left ungated, with rationale):\n  - "
            + "\n  - ".join(f"{f}::{n}({p})" for f, n, p in sorted(unclassified))
        )
    if stale:
        msg_parts.append(
            "Stale entries — declared in a surface set but no "
            "longer present in source. Remove them so the registry "
            "reflects the live surface:\n  - "
            + "\n  - ".join(f"{f}::{n}({p})" for f, n, p in sorted(stale))
        )

    assert not msg_parts, "\n\n".join(msg_parts)


def test_surface_sets_are_disjoint() -> None:
    """A surface belongs to exactly one set — sanity check on the registry itself."""
    names = sorted(_ALL_SETS)
    overlaps = {
        (a, b): sorted(_ALL_SETS[a] & _ALL_SETS[b])
        for i, a in enumerate(names)
        for b in names[i + 1 :]
        if _ALL_SETS[a] & _ALL_SETS[b]
    }
    assert not overlaps, f"Surfaces declared in more than one set — choose one: {overlaps}"


def _fn(source: str) -> ast.AsyncFunctionDef:
    node = ast.parse(source).body[0]
    assert isinstance(node, ast.AsyncFunctionDef)
    return node


@pytest.mark.parametrize(
    "body",
    [
        "    namespace = writable_namespace_arg(namespace)",
        "    if x:\n        namespace = constants.writable_namespace_arg(namespace)",
    ],
)
def test_rebinding_matcher_accepts(body: str) -> None:
    node = _fn(f"async def f(namespace):\n{body}\n")
    assert _rebinds_through_writable_namespace_arg(node, "namespace")


@pytest.mark.parametrize(
    "body",
    [
        # Result dropped: "" would flow on unnormalized.
        "    writable_namespace_arg(namespace)",
        # Bound to another name.
        "    ns = writable_namespace_arg(namespace)",
        # Another argument.
        "    namespace = writable_namespace_arg(other)",
        # Only inside a closure or class body that never runs as the surface.
        "    def inner(namespace):\n        namespace = writable_namespace_arg(namespace)",
        "    async def inner(namespace):\n        namespace = writable_namespace_arg(namespace)",
        "    class C:\n        namespace = writable_namespace_arg(namespace)",
    ],
)
def test_rebinding_matcher_rejects(body: str) -> None:
    node = _fn(f"async def f(namespace, other):\n{body}\n")
    assert not _rebinds_through_writable_namespace_arg(node, "namespace")
