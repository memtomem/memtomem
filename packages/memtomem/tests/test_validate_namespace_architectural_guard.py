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
* :func:`test_no_unclassified_ns_surfaces` — AST-scans
  ``server/tools/*.py`` for any tool that takes a namespace-shaped
  parameter. Every match must be classified into either set; an
  unclassified hit fails the test, forcing the author to make a decision
  rather than ship a silent gap.

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

# Surfaces the project has explicitly left ungated as of this writing.
# Each entry carries its rationale inline; if the deferral changes, move
# the triple to ``VALIDATED_NS_SURFACES`` and add the validator call to
# the function body in the same PR.
#
# Bulk deferral context — issue #500 "Out of scope":
#   > ``mem_add`` / ``mem_search`` ``namespace=`` arguments — broader UX
#   > call covered separately if pursued.
#
# That deferral covers the read-filter and per-write-path ``namespace=``
# arguments below. The forward-shield at session-start /
# ``mem_agent_share`` / ``mem_ns_*`` already prevents the bypass shape
# from landing in app state or session rows; gating these would be a UX
# decision (do we reject ``mem_add(namespace="foo bar")`` loudly, or
# accept the legacy charset?) and is tracked as future work.
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
        ("recall.py", "mem_recall", "namespace"),
        ("reflection.py", "mem_reflect", "namespace"),
        ("search.py", "mem_search", "namespace"),
        ("temporal.py", "mem_timeline", "namespace"),
        ("temporal.py", "mem_activity", "namespace"),
        # Write-path surfaces — caller-supplied namespace lands in chunks
        # rows. Deferred per the same issue-#500 "broader UX call" line;
        # the storage layer's ``_NS_NAME_RE`` (``[\\w\\-.:@ ]{1,255}``)
        # still rejects the most pathological shapes today, and tightening
        # it to the strict gate would break legacy-shape rows. Tracked for
        # follow-up.
        ("consolidation.py", "mem_consolidate", "namespace"),
        ("export_import.py", "mem_import", "namespace"),
        ("importers.py", "mem_import_notion", "namespace"),
        ("importers.py", "mem_import_obsidian", "namespace"),
        ("indexing.py", "mem_index", "namespace"),
        ("memory_crud.py", "_mem_add_core", "namespace"),
        ("memory_crud.py", "mem_add", "namespace"),
        ("memory_crud.py", "mem_delete", "namespace"),
        ("memory_crud.py", "mem_batch_add", "namespace"),
        ("procedure.py", "mem_procedure_save", "namespace"),
        ("url_index.py", "mem_fetch", "namespace"),
    }
)


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------


_TOOLS_DIR = pathlib.Path(__file__).resolve().parents[1] / "src" / "memtomem" / "server" / "tools"
_NS_PARAM_NAMES: frozenset[str] = frozenset({"namespace", "target", "old", "new", "old_namespace"})


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
    declared = VALIDATED_NS_SURFACES | DEFERRED_NS_SURFACES
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
            "``old_namespace=`` parameter must be added to either "
            "VALIDATED_NS_SURFACES (if it gates the input) or "
            "DEFERRED_NS_SURFACES (if the project explicitly leaves it "
            "ungated for now, with rationale):\n  - "
            + "\n  - ".join(f"{f}::{n}({p})" for f, n, p in sorted(unclassified))
        )
    if stale:
        msg_parts.append(
            "Stale entries — declared in VALIDATED/DEFERRED but no "
            "longer present in source. Remove them so the registry "
            "reflects the live surface:\n  - "
            + "\n  - ".join(f"{f}::{n}({p})" for f, n, p in sorted(stale))
        )

    assert not msg_parts, "\n\n".join(msg_parts)


def test_validated_and_deferred_are_disjoint() -> None:
    """A surface cannot be both gated and deferred — sanity check on
    the registry definition itself.
    """
    overlap = VALIDATED_NS_SURFACES & DEFERRED_NS_SURFACES
    assert not overlap, (
        f"Surfaces declared in BOTH validated and deferred sets — choose one: {sorted(overlap)}"
    )
