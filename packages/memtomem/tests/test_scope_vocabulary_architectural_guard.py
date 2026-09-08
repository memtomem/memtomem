"""Architectural guard: every read surface that takes a caller-supplied
ADR-0011 ``scope`` runs it through ``validate_scope_vocabulary``.

Why this exists (#2295). PR #2291 put the closed tier vocabulary
(``user`` / ``project_shared`` / ``project_local``) behind
:func:`memtomem.services.search_service.validate_scope_vocabulary` and
wired it into ``GET /api/search``, ``mem_search`` and ``mm search``. The
prose then said "the three search surfaces" in six places — and five
other surfaces (``mem_recall``, ``mm recall``, ``mem_timeline``,
``mem_entity_search``, ``mem_ask``) kept handing the raw string to
``ScopeFilter.parse``, so ``scope="User"`` was a refusal on one tool and
a successful empty result on the next. A hand-written list of "the
surfaces that validate" drifts the moment a new ``scope``-taking tool is
added; this file makes the contract declarative instead.

* :data:`VALIDATED_SCOPE_SURFACES` — every ``(module, function)`` whose
  body MUST call ``validate_scope_vocabulary(scope)`` and forward what
  it returns. Dropping the call trips
  :func:`test_declared_validated_surfaces_call_the_vocabulary_gate`.
* :data:`PERMISSIVE_SCOPE_SITES` — every site that deliberately does
  *not* validate, with the reason inline (the gate itself, the core
  predicate parser, portable eval cases, forwarders of an already
  validated value).
* :func:`test_no_unclassified_scope_sinks` — AST-scans the whole package
  for *scope sinks* and requires every hit to be classified. An
  unclassified sink fails, forcing the author to decide (gate it, or
  explain why not) rather than ship a silent gap.

Discovery keys on the **sink**, not on a parameter named ``scope``: the
context gateway (``cli/context_cmd.py``, ``server/tools/context.py``,
``web/routes/context_*``) and ``cli/pinned_cmd.py`` take ``scope``
arguments carrying an ADR-0011 *write* tier (``TargetScope``, a
``click.Choice``), not a filter string, and a name heuristic would drown
in them.

What counts as a sink, exactly — and what does not
--------------------------------------------------

Recognized (:func:`_is_scope_sink`):

* ``ScopeFilter.parse`` referenced by name, aliased
  (``from memtomem.models import ScopeFilter as SF`` → ``SF.parse``), or
  reached through a module (``models.ScopeFilter.parse``). The match is
  at *attribute* level rather than call level, so a parse bound to a
  local (``p = ScopeFilter.parse``) or handed to a callback still counts.
* A literal ``scope=`` keyword passed into a ``.search(...)`` or
  ``run_search(...)`` call — the pipeline parses the raw string itself,
  so forwarding one is the same sink as parsing one.

**Not** recognized, and deliberately so — this is the guard's advertised
boundary, pinned by :class:`TestDiscoveryBoundary` so that widening the
matcher forces this list to be rewritten with it:

* An indirectly bound search (``go = pipe.search; await go(scope=scope)``)
  or a splat (``pipe.search(**kwargs)``). Resolving either needs
  dataflow, not a syntactic walk.
* A wrapper that neither parses nor forwards, and only calls a helper
  that does. Its helper is classified; the wrapper is invisible.

So a *green* run means "no sink of a recognized shape is unclassified".
It does not mean "no unvalidated read surface can exist". A second
discovery axis over public read entry points would close that, and is
out of scope here (#2295 review).
"""

from __future__ import annotations

import ast
import pathlib

import pytest

# ---------------------------------------------------------------------------
# Surface registry
# ---------------------------------------------------------------------------

# Pairs are ``(path relative to src/memtomem, function name)``.

# Surfaces that MUST call ``validate_scope_vocabulary(scope)`` before they
# open a store or hand the value to the search core. Each one renders the
# refusal in its own idiom (returned ``"Error: …"`` string on MCP,
# ``click.ClickException`` on the CLI, ``422`` over HTTP) and forwards the
# *returned* value, so ``" user "`` is searched as ``user`` and ``""`` as
# no filter — never as ``scope IN (' user ')``.
VALIDATED_SCOPE_SURFACES: frozenset[tuple[str, str]] = frozenset(
    {
        # #2193 / #2291 — the original three search surfaces.
        ("server/tools/search.py", "mem_search"),
        ("cli/search.py", "_search"),
        ("web/routes/search.py", "search"),
        # #2295 — the read surfaces #2291 left on the parser-direct path.
        ("server/tools/recall.py", "mem_recall"),
        ("server/tools/temporal.py", "mem_timeline"),
        ("server/tools/entity.py", "mem_entity_search"),
        ("server/tools/ask.py", "mem_ask"),
        ("cli/memory.py", "_recall"),
        # The in-process backstop: every surface validates up front so it
        # can fail without opening anything, and ``run_search`` repeats the
        # check for callers that reach the core directly.
        ("services/search_service.py", "run_search"),
    }
)

# Sites the scan finds that deliberately do NOT validate. Each entry
# carries its rationale; if the reason stops holding, move the pair to
# ``VALIDATED_SCOPE_SURFACES`` and add the call in the same PR.
PERMISSIVE_SCOPE_SITES: frozenset[tuple[str, str]] = frozenset(
    {
        # The gate itself — it delegates the syntax parse to
        # ``ScopeFilter.parse`` before checking the vocabulary.
        ("services/search_service.py", "validate_scope_vocabulary"),
        # The core predicate parser. ``ScopeFilter.parse`` stays permissive
        # on purpose (a predicate builder, mirroring the open namespace
        # alphabet); the pipeline is reached only through surfaces that
        # validated, or through ``run_search`` which backstops.
        ("search/pipeline.py", "search"),
        # Portable eval cases: an unrecognized tier must deterministically
        # reach *no rows* rather than raise, because "matches no tier" is
        # exactly what makes a case promotable across projects
        # (``test_eval_cases.py::test_allows_user_and_non_project_scope``).
        ("storage/mixins/eval_cases.py", "validate_portable_filters"),
        ("storage/mixins/eval_cases.py", "_scope_implies_project"),
        # Replay hands a *stored* case's filters back to the pipeline
        # verbatim; the value was accepted when the run was recorded, and
        # rewriting history at replay time would change what is measured.
        ("quality/replay.py", "replay_cases"),
        # Not a surface of its own: forwards the ``effective_scope`` that
        # ``cli/search.py::_search`` already validated. Gating here would
        # double-validate the same value while leaving the caller's own
        # option no safer — the same reasoning as the ``_provenance.py``
        # forwarders in ``test_validate_namespace_architectural_guard.py``.
        ("cli/search.py", "_search_with_components"),
    }
)

# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------

_SRC_ROOT = pathlib.Path(__file__).resolve().parents[1] / "src" / "memtomem"
_GATE = "validate_scope_vocabulary"
_SEARCH_CALLEES: frozenset[str] = frozenset({"search", "run_search"})

FunctionNode = ast.AsyncFunctionDef | ast.FunctionDef


def _callee_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _scope_filter_names(tree: ast.Module) -> frozenset[str]:
    """Local names bound to ``models.ScopeFilter`` in this module.

    Always includes the canonical spelling, plus any ``import ... as``
    alias, so renaming the import on the way in does not hide a parse from
    the scan.
    """
    names = {"ScopeFilter"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "ScopeFilter" and alias.asname:
                    names.add(alias.asname)
    return frozenset(names)


def _iter_functions() -> list[tuple[str, FunctionNode, frozenset[str]]]:
    """Return ``(relative path, function node, ScopeFilter names)`` for every
    function under ``src/memtomem``. Private helpers participate too —
    ``cli/memory.py::_recall`` is where the CLI's scope actually lands.
    """
    out: list[tuple[str, FunctionNode, frozenset[str]]] = []
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        # Pin UTF-8: py312 resolves ``read_text`` from the locale, so the
        # scan would otherwise depend on the CI runner's environment.
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = _scope_filter_names(tree)
        rel = path.relative_to(_SRC_ROOT).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                out.append((rel, node, names))
    return out


def _parses_a_scope(sub: ast.AST, scope_filter_names: frozenset[str]) -> bool:
    """``<ScopeFilter-ish>.parse`` — bare, aliased, or module-qualified."""
    if not (isinstance(sub, ast.Attribute) and sub.attr == "parse"):
        return False
    owner = sub.value
    if isinstance(owner, ast.Name):
        return owner.id in scope_filter_names
    if isinstance(owner, ast.Attribute):
        # ``models.ScopeFilter.parse`` / ``memtomem.models.ScopeFilter.parse``
        return owner.attr == "ScopeFilter"
    return False


def _is_scope_sink(node: FunctionNode, scope_filter_names: frozenset[str]) -> bool:
    """True when the function body parses a scope string itself or hands a
    literal ``scope=`` keyword to the search core (which parses it).

    See the module docstring for the shapes this deliberately does not
    recognize; :class:`TestDiscoveryBoundary` pins both halves.
    """
    for sub in ast.walk(node):
        if _parses_a_scope(sub, scope_filter_names):
            return True
        if (
            isinstance(sub, ast.Call)
            and _callee_name(sub.func) in _SEARCH_CALLEES
            and any(kw.arg == "scope" for kw in sub.keywords)
        ):
            return True
    return False


def _calls_gate_on_scope(node: FunctionNode) -> bool:
    """True iff the body contains ``validate_scope_vocabulary(scope)`` —
    bare or as ``mod.validate_scope_vocabulary(scope)`` — with
    ``Name("scope")`` as the first positional argument.

    Deliberately strict, like the namespace guard's matcher: the keyword
    form ``validate_scope_vocabulary(scope=scope)``, a renamed local, and
    an aliased import do not match. The surfaces all write the call the
    same way; the strictness is what keeps "it validates" unambiguous.
    Strictness is safe *here* in a way it is not for discovery above: a
    missed gate call turns a declared surface red, while a missed sink
    would go silently green.

    What this predicate does **not** establish: that the call executes,
    that it runs before the store opens, or that its return value is what
    gets forwarded. A call inside an uncalled nested function, after
    initialization, or with its result discarded satisfies it. Those are
    behavioral properties, and every surface in
    :data:`VALIDATED_SCOPE_SURFACES` carries its own tests for them
    (``test_scope_param_wrappers.py``, ``test_cli_recall_scope.py``,
    ``test_cli_search_hints.py``, ``test_mem_search_wrapper.py``,
    ``test_web_routes.py``). A newly registered surface must bring the
    same pair — refused before opening, and the normalized value is what
    the acting layer receives — because registering it here does not
    confer them.
    """
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call) or _callee_name(sub.func) != _GATE:
            continue
        if sub.args and isinstance(sub.args[0], ast.Name) and sub.args[0].id == "scope":
            return True
    return False


def _sink_keys() -> list[tuple[str, str]]:
    """Every discovered sink's registry key, duplicates preserved."""
    return [
        (rel, node.name) for rel, node, names in _iter_functions() if _is_scope_sink(node, names)
    ]


def _found_sinks() -> set[tuple[str, str]]:
    return set(_sink_keys())


def _discovers(src: str) -> bool:
    """Run the discovery predicate over a synthetic module."""
    tree = ast.parse(src)
    names = _scope_filter_names(tree)
    return any(
        _is_scope_sink(node, names)
        for node in ast.walk(tree)
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rel, fn_name", sorted(VALIDATED_SCOPE_SURFACES))
def test_declared_validated_surfaces_call_the_vocabulary_gate(rel: str, fn_name: str) -> None:
    """Every entry in :data:`VALIDATED_SCOPE_SURFACES` must call
    ``validate_scope_vocabulary(scope)`` in its body."""
    matches = [node for path, node, _ in _iter_functions() if path == rel and node.name == fn_name]
    assert matches, (
        f"Declared validated surface not found in source: {rel}::{fn_name}. "
        "Either the function was renamed / removed, or VALIDATED_SCOPE_SURFACES is stale."
    )
    assert _calls_gate_on_scope(matches[0]), (
        f"{rel}::{fn_name} is declared a validated scope surface but its body does not "
        f"contain ``{_GATE}(scope)``. Restore the call ahead of opening the store (and "
        "forward what it returns), or move the entry to PERMISSIVE_SCOPE_SITES with a reason."
    )


def test_no_unclassified_scope_sinks() -> None:
    """Every scope sink in the package must be classified as validated or
    permissive. Unclassified *and* stale entries both fail, so the registry
    tracks the live surface in both directions."""
    declared = VALIDATED_SCOPE_SURFACES | PERMISSIVE_SCOPE_SITES
    found = _found_sinks()

    unclassified = found - declared
    stale = declared - found

    msg_parts = []
    if unclassified:
        msg_parts.append(
            "Unclassified scope sinks — every function that parses a scope string "
            "(``ScopeFilter.parse``) or passes ``scope=`` to the search core must be "
            "added to VALIDATED_SCOPE_SURFACES (it calls validate_scope_vocabulary) "
            "or PERMISSIVE_SCOPE_SITES (with rationale):\n  - "
            + "\n  - ".join(f"{rel}::{name}" for rel, name in sorted(unclassified))
        )
    if stale:
        msg_parts.append(
            "Stale entries — declared but no longer a scope sink in source:\n  - "
            + "\n  - ".join(f"{rel}::{name}" for rel, name in sorted(stale))
        )
    assert not msg_parts, "\n\n".join(msg_parts)


def test_validated_and_permissive_are_disjoint() -> None:
    overlap = VALIDATED_SCOPE_SURFACES & PERMISSIVE_SCOPE_SITES
    assert not overlap, f"Declared in BOTH sets — choose one: {sorted(overlap)}"


def test_sink_keys_identify_exactly_one_function_each() -> None:
    """``(module, function name)`` must address one function.

    Two same-named functions in one module — a module-level ``search`` and a
    method ``search``, say — collapse to a single key, so classifying one
    silently classifies the other, and the gate check above would grade only
    whichever ``matches[0]`` returned. Qualify the name or rename; do not
    let the pair share a key.
    """
    keys = _sink_keys()
    dupes = sorted({key for key in keys if keys.count(key) > 1})
    assert not dupes, "Registry keys that address more than one function:\n  - " + "\n  - ".join(
        f"{rel}::{name}" for rel, name in dupes
    )


def test_the_scan_reaches_every_validated_surface() -> None:
    """Floor for the matcher itself: a discovery that goes blind must go red,
    not green. Each validated surface has to be *found* as a sink, by name —
    otherwise a refactor that stops the scan from seeing (say) the CLI would
    let the unclassified check pass vacuously for that surface."""
    found = _found_sinks()
    missing = VALIDATED_SCOPE_SURFACES - found
    assert not missing, (
        "The sink scan no longer reaches these validated surfaces; fix the matcher "
        "before trusting the registry:\n  - "
        + "\n  - ".join(f"{rel}::{name}" for rel, name in sorted(missing))
    )


class TestDiscoveryBoundary:
    """What the scan sees, and what it is documented not to see.

    The module docstring advertises a guarantee, and a guarantee stated only
    in prose drifts from the matcher underneath it. Both halves are pinned:
    widening the matcher turns a "not recognized" case red and forces the
    docstring to be rewritten alongside it, and narrowing it turns a
    "recognized" case red.
    """

    @pytest.mark.parametrize(
        "label, src",
        [
            (
                "bare",
                "def f(scope):\n    return ScopeFilter.parse(scope)\n",
            ),
            (
                "aliased import",
                "from memtomem.models import ScopeFilter as SF\n"
                "def f(scope):\n    return SF.parse(scope)\n",
            ),
            (
                "module qualified",
                "def f(scope):\n    return models.ScopeFilter.parse(scope)\n",
            ),
            (
                "parse bound to a local",
                "def f(scope):\n    p = ScopeFilter.parse\n    return p(scope)\n",
            ),
            (
                "scope= into pipeline search",
                "async def f(scope):\n    return await pipe.search(query='q', scope=scope)\n",
            ),
            (
                "scope= into run_search",
                "async def f(scope):\n    return await run_search(query='q', scope=scope)\n",
            ),
        ],
    )
    def test_recognized_shapes_are_discovered(self, label: str, src: str) -> None:
        assert _discovers(src), f"discovery missed a shape it claims to cover: {label}"

    @pytest.mark.parametrize(
        "label, src",
        [
            (
                "search bound to a local",
                "async def f(scope):\n    go = pipe.search\n    return await go(scope=scope)\n",
            ),
            (
                "kwargs splat",
                "async def f(scope):\n"
                "    kw = {'scope': scope}\n"
                "    return await pipe.search(**kw)\n",
            ),
            (
                "wrapper delegating to a helper",
                "async def f(scope):\n    return await _shared_leg(scope)\n",
            ),
        ],
    )
    def test_documented_blind_spots_stay_blind(self, label: str, src: str) -> None:
        """These need dataflow, not a syntactic walk, so the guard says so
        rather than implying coverage it does not have. If you widen the
        matcher to catch one, delete its case here *and* the matching line in
        the module docstring's "not recognized" list — the guarantee and the
        code have to move together."""
        assert not _discovers(src), (
            f"discovery now catches {label!r}. That is an improvement, but the module "
            "docstring still advertises it as a blind spot — update both together."
        )
