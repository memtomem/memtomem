"""Architectural guard: every Gate B site records the consent it took.

ADR-0011 §5 pairs a refusal record with a consent record on every
``project_shared`` mutation. The refusal half shipped with the ADR; the
consent half — ``project_shared.confirmed_via=<surface>`` — was specified
and emitted by nothing at all, across six CLI, web, and MCP surfaces, until
#2306. A uniform gap is what made it survive: a reader who found the line
missing in one place had no reason to think it was present anywhere.

This file makes the pairing enforced rather than aspirational. It AST-scans
every module under ``src/memtomem`` for a conditional on
``confirm_project_shared`` and requires each one to be classified:

* a **gate** (negated form — ``not x``, ``x is False``, ``x == False``:
  shapes that refuse when consent is *absent*, and therefore let a write
  through when it is present) must be followed by a call to
  ``privacy.emit_project_shared_confirmation``;
* a gate whose consent is recorded by a different function is listed in
  :data:`DELEGATED_GATES` with the counterpart named;
* a **positive-form** conditional (``if x:``) authorises nothing — the two
  in the tree are argument-validation errors that refuse on the flag's
  *presence* — and is listed in :data:`NOT_A_GATE` with its reason.

An unclassified conditional fails. That is the point: a new surface
cannot quietly join the gated set *by taking the flag*.

The boundary of that claim, stated plainly because the first draft
overclaimed it. This scan finds a Gate B by the ``confirm_project_shared``
identifier, and the inventory it enforces was itself built from that same
identifier — so a Gate B expressed another way is invisible to both, and
the enumeration cannot certify itself. ``cli/context_cmd.py:pull_cmd`` used
to be the worked example of that: it gated a ``project_shared`` pull on
``--yes`` or a prompt per ADR-0030 §11, carried no ``confirm_project_shared``
at all, and emitted a consent line that could have been deleted without a
single assertion here going red. #2318 gave it the standard flag, so the scan
reaches it now.

That closed one hole, not the class. Two things are still true. Sites that
have *no* Gate B are invisible for the same reason a differently-spelled one
is — this scan has nothing to find in them. ADR-0011 §5's riders record the
population: the LangGraph ``MemtomemStore.add()`` adapter was one until #2321
gated it, and #2322's four derived-target writers still write to the
git-tracked tier having asked nobody. And what the scan buys
on a site it *does* reach is narrow: that the emit survives as long as the
conditional does. It does not check that ``--yes`` is refused, that the
emit's predicate mirrors the gate's, or that exactly one emit fires. Gate
*deletion* is out of scope too, with one deliberate exception — the surfaces
named in :func:`test_scan_reaches_the_sites_the_adr_names` are pinned as a
floor, so removing or respelling one of *those* gates does go red here. Every
other site can lose its gate silently as far as this file is concerned.

During the #2318 deprecation window this file
is green while ``mm context pull`` still accepts ``--yes``, and it would stay
green after the 0.7.0 flip if that acceptance were left in by accident. The
behavioural tests in ``test_cli_context_pull.py`` are what pin those.

Placement rule, and why it is strict. The emit must be a *later sibling*
of the gate in the gate's own statement list (or nested inside one), never
inside the gate's own body. The CLI shape makes the reason concrete::

    if scope == "project_shared" and not confirm_project_shared:
        if yes:
            raise click.ClickException(...)      # --yes is not consent
        if not _prompt_project_shared_confirm(target):
            raise click.Abort()                  # declined
        privacy.emit_project_shared_confirmation(...)   # WRONG

That body runs *only when the flag is absent*, so an emit inside it records
the prompt path and silently misses every ``--confirm-project-shared`` run —
half the consents, and the half a reviewer is most likely to be auditing.
The rule was written after exactly that bug appeared while implementing
#2306.

What this guard cannot check: whether the emit's *predicate* mirrors the
gate's. Several gates key on something other than the destination tier —
``git_tracked_write``, ``"project_shared" in scopes``, ``effective_scope``,
``scope_explicit and ...`` — and an emit guarded by the wrong one is
well-formed AST. The surface tests in ``test_privacy.py`` and the per-route
suites carry that half.

Matching is by NAME. An aliased import, a wrapper, or a ``functools.partial``
is not recognised; the convention is a direct
``privacy.emit_project_shared_confirmation(...)`` call in the gate's own
function. Gate *deletion* is likewise out of scope — that a surface gates
``project_shared`` at all is ADR-0011 §5's own invariant, pinned by the
per-surface refusal tests, not here.

Pattern lineage: ``feedback_ast_architectural_guard_pattern.md``
(unclassified-fails registry), ``test_context_atomic_write_guard.py``
(scan-list staleness check), ``test_web_invariants_registry.py`` (detector
self-tests).
"""

from __future__ import annotations

import ast
import pathlib
import textwrap
from collections.abc import Iterator

import pytest

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "memtomem"

_GATE_NAME = "confirm_project_shared"
_EMIT_NAME = "emit_project_shared_confirmation"

# ``(path relative to src/memtomem, function, surface it must forward)`` —
# gates whose consent line is emitted somewhere else. Each entry names the
# KIND of delegation and the function that emits; a bare path is not an entry.
#
# The third element is what keeps delegation honest. A shared implementation
# defaults to *some* surface, so a delegating caller that stops passing its
# own name does not fail — it silently files the consent under whichever
# caller the default belongs to. Naming the string here means dropping the
# argument breaks this guard rather than just producing a wrong record.
# ``None`` means the delegate forwards no surface (it emits nothing itself
# and its caller decides), so there is nothing to check.
DELEGATED_GATES: frozenset[tuple[str, str, str | None]] = frozenset(
    {
        # Pre-check → shared impl. Forwards confirm_project_shared into
        # cli/context_cmd._memory_migrate_run, which carries Gate B for both
        # surfaces; without the forwarded name it would record the CLI's.
        ("server/tools/context.py", "mem_context_memory_migrate", "mcp_context_memory_migrate"),
        # Pre-check → shared core. Forwards into memory_crud._mem_add_core,
        # which re-gates on the same derived scope; without the forwarded
        # name the consent would be filed under mem_add.
        ("server/tools/consolidation.py", "mem_consolidate_apply", "mem_consolidate_apply"),
        # Gate helper → its caller. Returns (flag, reason) and writes
        # nothing; transfer_context_artifact emits once the request is known
        # to be applying, naming itself.
        ("web/routes/context_transfer.py", "_required_confirm", None),
    }
)

#: ``(relpath, function)`` view of the registry, for the membership checks.
_DELEGATED_KEYS: frozenset[tuple[str, str]] = frozenset((rel, fn) for rel, fn, _ in DELEGATED_GATES)

# ``(path relative to src/memtomem, function)`` whose POSITIVE-form
# conditionals on the flag are not consent gates. Both entries are the same
# kind: an argument-validation error that refuses on the flag's *presence*
# ("--confirm-project-shared requires --to"), authorising nothing and
# therefore having no consent to record.
NOT_A_GATE: frozenset[tuple[str, str]] = frozenset(
    {
        ("server/tools/context.py", "mem_context_artifact_migrate"),
        ("cli/context_cmd.py", "migrate_cmd"),
    }
)

_FUNC_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)
_SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


def _src_files() -> list[pathlib.Path]:
    """Every production module the scan covers."""
    return sorted(_SRC.rglob("*.py"))


def _rel(path: pathlib.Path) -> str:
    """Registry key for *path*: always ``a/b.py``, never ``a\\b.py``.

    The registries above are written with forward slashes, and ``str()``
    on a Windows path gives backslashes — so on Windows every lookup
    missed, the classifications went unread, and all three scans failed
    at once. The separator is part of the key, so it is normalised in one
    place rather than at each call site.
    """
    return path.relative_to(_SRC).as_posix()


def _refs_flag(node: ast.AST) -> bool:
    """Does *node* mention ``confirm_project_shared`` as a value?"""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and sub.id == _GATE_NAME:
            return True
        if isinstance(sub, ast.Attribute) and sub.attr == _GATE_NAME:
            return True
    return False


def _is_negated(test: ast.expr) -> bool:
    """Is the flag consumed in a refuse-when-absent shape?

    ``not x``, ``x is False`` and ``x == False`` all mean "no consent
    given"; a bare ``if x:`` does not.
    """
    for sub in ast.walk(test):
        if isinstance(sub, ast.UnaryOp) and isinstance(sub.op, ast.Not):
            if _refs_flag(sub.operand):
                return True
        if isinstance(sub, ast.Compare) and _refs_flag(sub.left):
            for op, comparator in zip(sub.ops, sub.comparators):
                if isinstance(op, (ast.Is, ast.Eq)) and isinstance(comparator, ast.Constant):
                    if comparator.value is False:
                        return True
    return False


def _own_scope(node: ast.AST) -> Iterator[ast.AST]:
    """Walk *node*'s body without descending into code that cannot run here.

    Skips nested defs and lambdas (they may never be called) and the body
    of a statically false ``if`` (it never runs at all).
    """
    if isinstance(node, ast.If) and _is_statically_false(node.test):
        for child in node.orelse:
            yield child
            yield from _own_scope(child)
        return
    for child in ast.iter_child_nodes(node):
        if isinstance(child, _SCOPE_NODES):
            continue
        yield child
        yield from _own_scope(child)


def _is_emit(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr == _EMIT_NAME
    return isinstance(func, ast.Name) and func.id == _EMIT_NAME


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


def _statement_lists(tree: ast.AST) -> Iterator[list[ast.stmt]]:
    """Every statement list in *tree* (module body, if/try/with bodies, …)."""
    for node in ast.walk(tree):
        for field in ("body", "orelse", "finalbody"):
            block = getattr(node, field, None)
            if isinstance(block, list) and block and isinstance(block[0], ast.stmt):
                yield block


def _emit_follows(tree: ast.AST, gate: ast.If) -> bool:
    """Is the gate's consent recorded on the path where consent exists?

    Two placements qualify: a later sibling in the statement list that
    holds the gate, or the gate's own ``else`` branch. Both run only when
    the gate did not refuse.

    The gate's ``body`` is deliberately excluded — it runs only when
    consent is absent (see the module docstring) — and so is any nested
    ``def``, which is code that may never be called.

    An emit that *cannot* run does not count either. Being syntactically
    later is not enough: a call sitting after an unconditional ``return``
    is dead, and one under a statically false test never fires. Both
    satisfied an earlier version of this rule, which meant the guard
    could be silenced by moving a line rather than by writing the record.
    """
    if any(_is_emit(sub) for branch in gate.orelse for sub in _own_scope_incl(branch)):
        return True
    for block in _statement_lists(tree):
        for index, stmt in enumerate(block):
            if stmt is not gate:
                continue
            for later in block[index + 1 :]:
                if isinstance(later, _SCOPE_NODES):
                    continue
                if _is_unconditional_exit(later):
                    break  # everything past this point is dead
                if any(_is_emit(sub) for sub in _own_scope_incl(later)):
                    return True
    return False


def _is_unconditional_exit(stmt: ast.stmt) -> bool:
    """Does *stmt* end the function outright, making later siblings dead?"""
    return isinstance(stmt, (ast.Return, ast.Raise, ast.Continue, ast.Break))


def _is_statically_false(test: ast.expr) -> bool:
    """``if False:`` / ``if 0:`` — a branch a reader can see never runs.

    Not reachability analysis, which this guard cannot do in general (that
    is what the behavioural tests are for). It refuses only the trivial
    dodge of parking the emit somewhere it can never fire.
    """
    return isinstance(test, ast.Constant) and not test.value


def _own_scope_incl(node: ast.AST) -> Iterator[ast.AST]:
    """``_own_scope`` plus *node* itself."""
    yield node
    yield from _own_scope(node)


def _candidates(tree: ast.AST) -> list[ast.If]:
    return [n for n in ast.walk(tree) if isinstance(n, ast.If) and _refs_flag(n.test)]


def _offenses(
    tree: ast.AST,
    rel: str,
    *,
    delegated: frozenset[tuple[str, str]] = _DELEGATED_KEYS,
    not_a_gate: frozenset[tuple[str, str]] = NOT_A_GATE,
) -> list[str]:
    """Unclassified or unrecorded Gate B conditionals in one module."""
    offenses: list[str] = []
    for gate in _candidates(tree):
        fn = _enclosing_function(tree, gate.lineno)
        where = f"{rel}:{gate.lineno} ({fn})"
        if _is_negated(gate.test):
            if (rel, fn) in delegated:
                continue
            if not _emit_follows(tree, gate):
                inside = any(_is_emit(sub) for sub in _own_scope(gate))
                reason = (
                    "the only emit sits INSIDE the gate body, which runs solely when "
                    "consent is absent — it would miss every confirmed run"
                    if inside
                    else f"no {_EMIT_NAME} call after the gate in its block"
                )
                offenses.append(f"{where} {reason}")
        elif (rel, fn) not in not_a_gate:
            offenses.append(
                f"{where} positive-form conditional on {_GATE_NAME} is unclassified — "
                f"if it authorises a write it must emit, otherwise add a NOT_A_GATE entry"
            )
    return offenses


def _snippet_offenses(source: str, **kwargs) -> list[str]:
    return _offenses(
        ast.parse(textwrap.dedent(source)),
        "snippet.py",
        delegated=kwargs.pop("delegated", frozenset()),
        not_a_gate=kwargs.pop("not_a_gate", frozenset()),
    )


# ── the scan ──────────────────────────────────────────────────────────────


def test_every_gate_b_site_records_its_consent() -> None:
    offenses: list[str] = []
    for path in _src_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        offenses.extend(_offenses(tree, _rel(path)))
    assert not offenses, (
        "Gate B site without an ADR-0011 §5 consent record. Call "
        "privacy.emit_project_shared_confirmation after the gate falls through "
        "(never inside its body), or classify the site in DELEGATED_GATES / "
        "NOT_A_GATE with a reason:\n  " + "\n  ".join(offenses)
    )


def test_registry_entries_are_not_stale() -> None:
    """A renamed or deleted site must fail loudly, not pass by vacancy."""
    seen: dict[tuple[str, str], list[ast.If]] = {}
    for path in _src_files():
        rel = _rel(path)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for gate in _candidates(tree):
            seen.setdefault((rel, _enclosing_function(tree, gate.lineno)), []).append(gate)
    for entry in sorted(_DELEGATED_KEYS | NOT_A_GATE):
        assert entry in seen, (
            f"registry entry {entry} names no conditional on {_GATE_NAME} any more — "
            "the site was renamed, moved, or removed; update the registry"
        )


def test_delegated_gates_do_not_also_emit() -> None:
    """A delegated site must not carry its own line — that double-records."""
    for rel, fn in sorted(_DELEGATED_KEYS):
        tree = ast.parse((_SRC / rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, _FUNC_NODES) and node.name == fn:
                assert not any(_is_emit(sub) for sub in _own_scope(node)), (
                    f"{rel}::{fn} is registered as delegating its consent line but "
                    "also emits one — the consent would be recorded twice"
                )


def test_delegated_gates_forward_their_own_surface() -> None:
    """A delegate must tell the shared implementation whose consent this is.

    The implementation it hands off to has a default surface, so dropping
    the forwarded name does not fail anything by itself — it just files
    the consent under the wrong caller. Pinning the literal here means the
    argument cannot be removed silently.
    """
    for rel, fn, expected in sorted(DELEGATED_GATES):
        if expected is None:
            continue
        tree = ast.parse((_SRC / rel).read_text(encoding="utf-8"))
        forwarded: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, _FUNC_NODES) or node.name != fn:
                continue
            for sub in _own_scope(node):
                if not isinstance(sub, ast.Call):
                    continue
                for kw in sub.keywords:
                    if kw.arg in {"surface", "confirmation_surface", "consent_surface"} and (
                        isinstance(kw.value, ast.Constant)
                    ):
                        forwarded.add(str(kw.value.value))
        assert expected in forwarded, (
            f"{rel}::{fn} delegates its ADR-0011 §5 consent line but no longer forwards "
            f"{expected!r} — the shared implementation would record its own default "
            f"surface instead, filing this call under the wrong one. Forwarded: "
            f"{sorted(forwarded) or 'nothing'}"
        )


def test_registry_keys_are_separator_independent() -> None:
    """The scan's keys must match the registries on every platform.

    ``str()`` on a relative Windows path yields ``cli\\context_cmd.py``,
    which matches none of the forward-slash entries below — so on Windows
    the classifications silently went unread and every scan failed at
    once, while macOS and Linux stayed green. A witness that runs
    everywhere: the keys the scan produces never contain a backslash, and
    the registries do not either.
    """
    for path in _src_files():
        assert "\\" not in _rel(path), f"scan key is not POSIX-shaped: {_rel(path)!r}"
    for rel, _fn, _surface in DELEGATED_GATES:
        assert "\\" not in rel, rel
    for rel, _fn in NOT_A_GATE:
        assert "\\" not in rel, rel


def test_scan_reaches_the_sites_the_adr_names() -> None:
    """An empty scan is green forever; pin that it sees the named surfaces.

    ADR-0011 §5 names ``mm mem add`` and the MCP write tool explicitly, so
    those two are the floor. Not a count: counts rot on every legitimate
    addition, and the detector is the pin.

    ``pull_cmd`` is here for a second reason. It was the one site the scan
    could not see (#2318 gave it the flag), and the module docstring says so
    — if the gate were spelled back into a ``--yes``-only shape, or deleted,
    every other assertion in this file would stay green. This is the line
    that goes red.

    ``settings_migrate_cmd`` is here for the same reason and a sharper one:
    it had no Gate B at all (#2348), so it was invisible to this scan the way
    an *absent* gate always is. It also ships the only predicate in the file
    that fires on a **source** tier, so a regression that narrowed the gate
    back to the destination would leave the flag in place — and this
    assertion would still pass. Read it as pinning the site, not the
    predicate; ``TestSettingsMigrateGateB`` pins what the predicate covers.
    """
    found: set[tuple[str, str]] = set()
    for path in _src_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for gate in _candidates(tree):
            found.add((_rel(path), _enclosing_function(tree, gate.lineno)))
    assert ("cli/memory.py", "_add") in found
    assert ("server/tools/memory_crud.py", "_mem_add_core") in found
    assert ("cli/context_cmd.py", "pull_cmd") in found
    assert ("cli/context_cmd.py", "settings_migrate_cmd") in found


# ── detector self-tests ───────────────────────────────────────────────────

_GATE_AND_EMIT = """
    def f(scope, confirm_project_shared):
        if scope == "project_shared" and not confirm_project_shared:
            return "refused"
        privacy.emit_project_shared_confirmation(surface="s", mechanism="param")
        write()
"""


def test_detector_accepts_emit_after_the_gate() -> None:
    assert _snippet_offenses(_GATE_AND_EMIT) == []


def test_detector_rejects_gate_without_emit() -> None:
    offenses = _snippet_offenses("""
        def f(scope, confirm_project_shared):
            if scope == "project_shared" and not confirm_project_shared:
                return "refused"
            write()
    """)
    assert len(offenses) == 1
    assert "no emit_project_shared_confirmation call" in offenses[0]
    assert "(f)" in offenses[0]


def test_detector_rejects_emit_inside_the_gate_body() -> None:
    """The bug this rule exists for: the body runs only without consent."""
    offenses = _snippet_offenses("""
        def f(scope, confirm_project_shared, yes, target):
            if scope == "project_shared" and not confirm_project_shared:
                if yes:
                    raise ClickException("--yes is not consent")
                if not prompt(target):
                    raise Abort()
                privacy.emit_project_shared_confirmation(surface="s", mechanism="prompt")
            write()
    """)
    assert len(offenses) == 1
    assert "INSIDE the gate body" in offenses[0]


def test_detector_rejects_emit_placed_before_the_gate() -> None:
    offenses = _snippet_offenses("""
        def f(scope, confirm_project_shared):
            privacy.emit_project_shared_confirmation(surface="s", mechanism="param")
            if scope == "project_shared" and not confirm_project_shared:
                return "refused"
            write()
    """)
    assert len(offenses) == 1


def test_detector_requires_one_emit_per_gate_in_sibling_branches() -> None:
    """``mem_context_version``'s shape: two gates, two consents."""
    offenses = _snippet_offenses("""
        def f(action, scope, confirm_project_shared):
            if action == "enable":
                if scope == "project_shared" and not confirm_project_shared:
                    return "refused"
                enable()
            if scope == "project_shared" and not confirm_project_shared:
                return "refused"
            privacy.emit_project_shared_confirmation(surface="s", mechanism="param")
            create()
    """)
    # The nested gate is the one left unrecorded; the emit after the second
    # gate must not be read as covering it.
    assert len(offenses) == 1
    assert "no emit_project_shared_confirmation call" in offenses[0]


def test_detector_requires_one_emit_per_gate_in_nested_blocks() -> None:
    """``mem_delete``'s shape: three gates under three separate locks."""
    offenses = _snippet_offenses("""
        def f(chunk, source, confirm_project_shared):
            if chunk:
                with lock():
                    if scope(chunk) == "project_shared" and not confirm_project_shared:
                        return "refused"
                    delete(chunk)
            if source:
                with lock():
                    if "project_shared" in scopes(source) and not confirm_project_shared:
                        return "refused"
                    delete(source)
            privacy.emit_project_shared_confirmation(surface="s", mechanism="param")
    """)
    assert len(offenses) == 2


def test_detector_accepts_emit_nested_in_a_later_sibling() -> None:
    assert (
        _snippet_offenses("""
        def f(scope, confirm_project_shared):
            if scope == "project_shared" and not confirm_project_shared:
                return "refused"
            if scope == "project_shared":
                privacy.emit_project_shared_confirmation(surface="s", mechanism="param")
            write()
    """)
        == []
    )


def test_detector_accepts_emit_in_the_gates_else_branch() -> None:
    assert (
        _snippet_offenses("""
        def f(scope, confirm_project_shared):
            if scope == "project_shared" and not confirm_project_shared:
                return "refused"
            else:
                privacy.emit_project_shared_confirmation(surface="s", mechanism="param")
    """)
        == []
    )


@pytest.mark.parametrize("holder", ["body", "req"])
def test_detector_sees_attribute_form_gates(holder: str) -> None:
    """Four web routes gate on ``body.``/``req.``, not a bare name."""
    offenses = _snippet_offenses(f"""
        def f({holder}):
            if {holder}.to_scope == "project_shared" and not {holder}.confirm_project_shared:
                return envelope()
            write()
    """)
    assert len(offenses) == 1


@pytest.mark.parametrize(
    "test_src",
    [
        "scope_explicit and scope == 'project_shared' and not confirm_project_shared",
        "confirm_project_shared is False",
        "confirm_project_shared == False",
        "not confirm_project_shared",
    ],
)
def test_detector_sees_every_refuse_when_absent_shape(test_src: str) -> None:
    """A gate rewritten in a new shape must not vanish from the scan."""
    offenses = _snippet_offenses(f"""
        def f(scope, scope_explicit, confirm_project_shared):
            if {test_src}:
                return "refused"
            write()
    """)
    assert len(offenses) == 1


def test_detector_flags_an_unclassified_positive_form() -> None:
    offenses = _snippet_offenses("""
        def f(confirm_project_shared):
            if confirm_project_shared:
                raise UsageError("--confirm-project-shared requires --to")
    """)
    assert len(offenses) == 1
    assert "unclassified" in offenses[0]


def test_detector_honours_a_not_a_gate_entry() -> None:
    assert (
        _snippet_offenses(
            """
        def f(confirm_project_shared):
            if confirm_project_shared:
                raise UsageError("--confirm-project-shared requires --to")
    """,
            not_a_gate=frozenset({("snippet.py", "f")}),
        )
        == []
    )


def test_detector_honours_a_delegated_entry() -> None:
    assert (
        _snippet_offenses(
            """
        def f(scope, confirm_project_shared):
            if scope == "project_shared" and not confirm_project_shared:
                return "refused"
            delegate(confirm_project_shared=confirm_project_shared)
    """,
            delegated=frozenset({("snippet.py", "f")}),
        )
        == []
    )


def test_detector_ignores_passthrough_and_prose() -> None:
    """Kwargs, ternaries, field declarations and docstrings are not gates."""
    assert (
        _snippet_offenses('''
        def f(confirm_project_shared):
            """Pass confirm_project_shared=True; `not confirm_project_shared` refuses."""
            mechanism = "flag" if confirm_project_shared else "prompt"
            delegate(confirm_project_shared=confirm_project_shared, mechanism=mechanism)
            hint = "re-run with confirm_project_shared=true"
            return hint
    ''')
        == []
    )


def test_detector_ignores_an_emit_in_an_uncalled_nested_def() -> None:
    offenses = _snippet_offenses("""
        def f(scope, confirm_project_shared):
            if scope == "project_shared" and not confirm_project_shared:
                return "refused"

            def _later():
                privacy.emit_project_shared_confirmation(surface="s", mechanism="param")

            write()
    """)
    assert len(offenses) == 1


def test_detector_rejects_emit_that_is_dead_after_a_return() -> None:
    """Syntactically later is not the same as reachable."""
    offenses = _snippet_offenses("""
        def f(scope, confirm_project_shared):
            if scope == "project_shared" and not confirm_project_shared:
                return "refused"
            return "written"
            privacy.emit_project_shared_confirmation(surface="s", mechanism="param")
    """)
    assert len(offenses) == 1


def test_detector_rejects_emit_parked_under_a_false_constant() -> None:
    offenses = _snippet_offenses("""
        def f(scope, confirm_project_shared):
            if scope == "project_shared" and not confirm_project_shared:
                return "refused"
            if False:
                privacy.emit_project_shared_confirmation(surface="s", mechanism="param")
            write()
    """)
    assert len(offenses) == 1


def test_detector_still_cannot_judge_the_emit_predicate() -> None:
    """The documented boundary, pinned so it is not mistaken for coverage.

    An emit under an inverted tier test is well-formed AST and passes.
    Deciding predicate equivalence would false-reject the real sites that
    key on ``git_tracked_write`` or ``"project_shared" in scopes``, so the
    behavioural tests carry this half — see the module docstring.
    """
    assert (
        _snippet_offenses("""
        def f(scope, confirm_project_shared):
            if scope == "project_shared" and not confirm_project_shared:
                return "refused"
            if scope != "project_shared":
                privacy.emit_project_shared_confirmation(surface="s", mechanism="param")
            write()
    """)
        == []
    )


def test_detector_accepts_the_bare_imported_name() -> None:
    assert (
        _snippet_offenses("""
        def f(scope, confirm_project_shared):
            if scope == "project_shared" and not confirm_project_shared:
                return "refused"
            emit_project_shared_confirmation(surface="s", mechanism="param")
    """)
        == []
    )
