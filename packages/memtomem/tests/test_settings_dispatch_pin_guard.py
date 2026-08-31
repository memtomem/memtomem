"""Every threaded settings-engine dispatch carries the guards its worker needs.

``asyncio.to_thread`` cannot be cancelled, so a worker outlives the request
that started it, and the context managers here are what keep that worker
accountable to the caller it has already outlived. ``pinned_host_homes()``
decides *where* a late write lands (#2211): every user-scope settings target is
anchored on the ambient ``$HOME`` / ``$KIMI_CODE_HOME``, so a dispatch that
forgets it lets the write follow the environment to a home its caller never
chose — which is how a cancelled sync came to write the developer's real
``~/.claude/settings.json`` mid-suite. ``abandon_sync_on_exit()`` decides
*whether* it happens at all (#2218): without it a timed-out request returns
503 and then mutates the user's settings seconds later. Sitting inside them is
necessary but not sufficient: the hand-off must also be one that copies the
caller's context, which ``asyncio.to_thread`` does and ``run_in_executor``
does not.

Which guards apply is **per callable**, not global (#2247). Every entry owes
the abort flag; the pin is owed only by a callable that resolves host homes
inside its worker. ``generate_all_settings`` does, and is pinned. The settings
siblings and the CRUD closures do not — they take paths their caller resolved
on the event loop — so demanding a pin of them would be cargo-culting a rule
whose rationale does not reach them. The skills engines owe both: they
resolve user homes in the worker through ``scope_resolver`` /
``_runtime_targets``, which read the pin as of #2250. Hence
:data:`_DISPATCH_RULES` rather than one list of scopes.

A callable's presence in that table is itself a claim: that its engine has
abort checks placed against its own transaction boundaries, so handing it the
flag means something.

Per-site regression tests cannot cover a dispatcher that does not exist yet, so
the rule is enforced lexically over the tree instead: the guard finds the call
sites itself rather than reading a hand-kept list of *sites* that a new
dispatcher would silently sit outside of.

That promise holds only for the spellings the matcher can see, so it resolves
through four indirections rather than reading what a call literally spells
(#2249): an alias (``run = generate_all_settings``), a lambda, a nested ``def``
passed by name, and ``functools.partial`` — each of them equally when stored in
a local first (``job = partial(engine, root)``). Every one hands the engine to a
worker exactly as the bare name does.

Resolving more names is only an improvement while it stays honest about what
the code actually runs, so the matcher reads bindings **per lexical scope**
(the same local name means different things in two functions) and summarises a
wrapper by what its body *calls*, not by everything lexically inside it (a
nested function it only defines runs nothing). ``functools.partial`` is proven
from the imports rather than from the callee's name, so a local ``def partial``
that dispatches without copying the context is still caught. The fixtures below
exercise every shape in both directions — a matcher that only rejects more is a
bigger surface to be wrong on, not a better guard.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src" / "memtomem"

_ABANDON = ("abandon_sync_on_exit", "#2218")
_PIN = ("pinned_host_homes", "#2211")

#: Dispatched callable → the context managers a threaded hand-off of it must
#: sit inside, each with the issue that put it there (the offender line names
#: the issue so a failure points at the rationale, not just a missing call).
#:
#: The pin is owed by the entries that resolve ``$HOME``-anchored targets
#: inside the worker: ``generate_all_settings`` (#2211) and the two skills
#: engines (#2250). The rest take paths their caller resolved on the event
#: loop, so there is nothing for a pin to redirect.
_DISPATCH_RULES: dict[str, tuple[tuple[str, str], ...]] = {
    "generate_all_settings": (_PIN, _ABANDON),
    "apply_hook_copy": (_ABANDON,),
    # No threaded dispatcher today (CLI only). Listed so the first one that
    # appears fails here until it is wrapped, rather than shipping a pair-lock
    # transaction that can write behind a 503 (#2247).
    "apply_migration": (_ABANDON,),
    "_locked_cas_write": (_ABANDON,),
    # Skills engines (#2247 abort, #2250 pin). Both resolve user-scope homes
    # inside the worker — `scope_resolver.canonical_artifact_dir` and
    # `_runtime_targets.runtime_fanout_root` expand `~` there — so they carry
    # the #2211 shape and, since #2250, the pin that answers it.
    "generate_all_skills": (_PIN, _ABANDON),
    "extract_skills_to_canonical": (_PIN, _ABANDON),
    # One target, one write, and ``.mcp.json`` is project-rooted from an
    # argument — abort-only, with nothing home-anchored to pin (#2247).
    "generate_all_mcp_servers": (_ABANDON,),
    "_create_locked": (_ABANDON,),
    "_update_locked": (_ABANDON,),
    "_delete_locked": (_ABANDON,),
}


#: Scopes that own a name table. ``ClassDef`` is one, and deliberately not
#: visible to functions nested in it — a method resolves globals through the
#: module, not through the class body (#2249 review).
_SCOPE_TYPES = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)

#: A name bound to something this guard cannot follow — a parameter, a loop or
#: comprehension target, an augmented assignment, a ``global``/``nonlocal``
#: declaration, an unpacking whose shape does not line up. Recorded rather than
#: skipped: the point is that the binding *exists*, so an outer alias of the
#: same name must not shine through it and invent a dispatch.
_OPAQUE = object()

#: Bindings created by ``import functools`` and ``from functools import partial``
#: (under whatever alias). Tracked apart from :data:`_OPAQUE` so the partial
#: exemption can be proven at the *use site*: a module that imports ``partial``
#: and then defines its own function of that name dispatches through the local
#: one, and that hand-off must stay caught (#2249 review).
_IMPORTED_FUNCTOOLS = object()
_IMPORTED_PARTIAL = object()


def _bound_names(target: ast.AST) -> list[str]:
    """Every plain name an assignment target binds, unpacking included."""
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, ast.Starred):
        return _bound_names(target.value)
    if isinstance(target, (ast.Tuple, ast.List)):
        return [name for element in target.elts for name in _bound_names(element)]
    return []


class _Scopes:
    """Lexical scopes, their local bindings, and which scope owns each node.

    A module-wide name table would be wrong in both directions (#2249 review):
    two functions using the same local name (``run = harmless`` here,
    ``run = generate_all_settings`` there) would make an innocent call look
    like a dispatch, or hide a real one, depending on walk order. Bindings are
    therefore recorded per scope and resolved from the use site outwards.

    This stays a lexical approximation and does not become a dataflow engine —
    the issue asks for the wrapper shapes a contributor plausibly writes, not
    for a Python semantic analyzer. Two consequences are deliberate and
    documented rather than fixed:

    * **Order within a scope is ignored.** A name bound to a guarded engine
      anywhere in a scope is read as that engine at every use in it. Rebinding
      the same local to two different callables around a dispatch is the shape
      this over-approximates, and over-reporting is the direction a guard
      should be wrong in — it fails loudly on the next contributor's screen
      rather than silently.
    * **Anything else that binds a name makes it opaque**, never transparent:
      the outer alias stops being visible, so an unfollowable binding costs a
      missed report at worst, never an invented one.
    """

    def __init__(self, tree: ast.AST) -> None:
        self.parent: dict[int, ast.AST | None] = {}
        self.bindings: dict[int, dict[str, list[object]]] = {}
        self.owner: dict[int, ast.AST] = {}
        self._build(tree, None)

    def _build(self, scope: ast.AST, parent: ast.AST | None) -> None:
        self.parent[id(scope)] = parent
        bindings: dict[str, list[object]] = {}

        def bind(name: str, value: object) -> None:
            bindings.setdefault(name, []).append(value)

        # A function's own parameters shadow every outer alias of that name.
        if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            args = scope.args
            for arg in [
                *args.posonlyargs,
                *args.args,
                *args.kwonlyargs,
                *([args.vararg] if args.vararg else []),
                *([args.kwarg] if args.kwarg else []),
            ]:
                bind(arg.arg, _OPAQUE)

        for node in self._own_nodes(scope):
            self.owner[id(node)] = scope
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                bind(node.name, node)
            elif isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
                for target in targets:
                    # ``a, b = f, g`` lines up; anything else is opaque, so an
                    # unpacked name cannot inherit an outer alias.
                    if isinstance(target, (ast.Tuple, ast.List)) and isinstance(
                        value, (ast.Tuple, ast.List)
                    ):
                        if len(target.elts) == len(value.elts):
                            for element, element_value in zip(target.elts, value.elts):
                                for name in _bound_names(element):
                                    bind(name, element_value)
                            continue
                    for name in _bound_names(target):
                        bind(name, value if isinstance(target, ast.Name) else _OPAQUE)
            elif isinstance(node, ast.AnnAssign):
                if node.value is not None:
                    for name in _bound_names(node.target):
                        bind(name, node.value)
            elif isinstance(node, (ast.AugAssign, ast.NamedExpr)):
                for name in _bound_names(node.target):
                    bind(name, _OPAQUE)
            elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
                for name in _bound_names(node.target):
                    bind(name, _OPAQUE)
            elif isinstance(node, ast.withitem) and node.optional_vars is not None:
                for name in _bound_names(node.optional_vars):
                    bind(name, _OPAQUE)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                bind(node.name, _OPAQUE)
            elif isinstance(node, (ast.Global, ast.Nonlocal)):
                # The name is rebound somewhere this guard does not follow.
                for name in node.names:
                    bind(name, _OPAQUE)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    bound = alias.asname or alias.name.split(".")[0]
                    bind(bound, _IMPORTED_FUNCTOOLS if alias.name == "functools" else _OPAQUE)
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    from_functools_partial = node.module == "functools" and alias.name == "partial"
                    bind(
                        alias.asname or alias.name,
                        _IMPORTED_PARTIAL if from_functools_partial else _OPAQUE,
                    )

        self.bindings[id(scope)] = bindings
        for node in self._own_nodes(scope):
            if isinstance(node, _SCOPE_TYPES):
                self._build(node, scope)

    @staticmethod
    def _own_nodes(scope: ast.AST) -> list[ast.AST]:
        """Nodes lexically in ``scope`` but not inside a nested scope of it.

        Stopping at the boundary is what keeps a merely *defined* nested
        function from being read as an executed one: ``def outer(): def
        unused(): generate_all_settings(...)`` calls nothing (#2249 review).

        A nested definition's decorators, defaults and annotations are the
        exception, and belong here: Python evaluates them in the *enclosing*
        scope, so reading them as part of the function's own body would let a
        body-local rebinding hide or invent a decorator-site dispatch.
        """
        out: list[ast.AST] = []
        # This scope's own decorators/defaults belong to its parent, which
        # already collected them; re-collecting here would let a body-local
        # rebinding shadow the name the decorator actually sees.
        parent_owned = {id(node) for node in _evaluated_in_parent(scope)}

        def walk(node: ast.AST) -> None:
            for child in ast.iter_child_nodes(node):
                if id(child) in parent_owned:
                    continue
                out.append(child)
                if isinstance(child, _SCOPE_TYPES):
                    for evaluated_outside in _evaluated_in_parent(child):
                        out.append(evaluated_outside)
                        walk(evaluated_outside)
                    continue
                walk(child)

        walk(scope)
        return out

    def scope_of(self, node: ast.AST) -> ast.AST | None:
        return self.owner.get(id(node))

    def lookup(self, name: str, scope: ast.AST | None) -> list[tuple[object, ast.AST]]:
        """Bindings for ``name`` visible from ``scope``, innermost scope wins.

        Class bodies are skipped on the way out — a name in a method resolves
        against the module, never against the surrounding class (#2249 review)
        — except when the use site is the class body itself.
        """
        first = True
        while scope is not None:
            if first or not isinstance(scope, ast.ClassDef):
                found = self.bindings[id(scope)].get(name)
                if found is not None:
                    return [(value, scope) for value in found]
            first = False
            scope = self.parent[id(scope)]
        return []


def _evaluated_in_parent(scope: ast.AST) -> list[ast.AST]:
    """A nested definition's parts that the *enclosing* scope evaluates."""
    if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
        args = scope.args
        return [
            *scope.decorator_list,
            *args.defaults,
            *[d for d in args.kw_defaults if d is not None],
            *([scope.returns] if scope.returns else []),
        ]
    if isinstance(scope, ast.Lambda):
        args = scope.args
        return [*args.defaults, *[d for d in args.kw_defaults if d is not None]]
    if isinstance(scope, ast.ClassDef):
        return [*scope.decorator_list, *scope.bases, *[kw.value for kw in scope.keywords]]
    return []


def _reference_name(node: ast.AST) -> str | None:
    """The bare name a reference expression spells, if it is one.

    Both spellings: the bare name, and a qualified reference such as
    ``settings.generate_all_settings``. Matching only the bare name would let
    one import-style hop carry a dispatcher out of view. Only a *bare* name is
    ever resolved through the binding tables, though — an attribute's receiver
    is not modelled, so treating ``worker.run`` as a local ``run`` would invent
    dispatches (#2249 review). See :func:`_direct_name`.
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _direct_name(node: ast.AST) -> str | None:
    """The guarded callable this expression names outright, if any."""
    name = _reference_name(node)
    return name if name in _DISPATCH_RULES else None


class _Matcher:
    """Resolves what a hand-off argument would actually call.

    Four indirections stand between a dispatch and the name a matcher reading
    only the call's arguments can see (#2249): an alias, a lambda, a nested
    ``def`` passed by name, and ``functools.partial`` — each of them equally so
    when stored in a local first (``job = partial(engine, root)``).
    """

    def __init__(self, tree: ast.AST) -> None:
        self.scopes = _Scopes(tree)

    def is_partial(self, node: ast.AST, scope: ast.AST | None) -> bool:
        """Whether this call is the stdlib ``functools.partial``.

        Proven from the imports, and then from the *bindings visible at the use
        site* (#2249 review): a module that imports ``partial`` and then
        defines its own function of that name dispatches through the local one,
        which is exactly the unsafe hand-off the guard must keep catching.
        ``partialmethod`` gets no exemption — no dispatch site uses it, and a
        rule with no call site is a rule nobody can check.
        """
        if not isinstance(node, ast.Call):
            return False
        func = node.func
        if isinstance(func, ast.Name):
            visible = self.scopes.lookup(func.id, scope)
            return bool(visible) and all(value is _IMPORTED_PARTIAL for value, _s in visible)
        if not (
            isinstance(func, ast.Attribute)
            and func.attr == "partial"
            and isinstance(func.value, ast.Name)
        ):
            return False
        visible = self.scopes.lookup(func.value.id, scope)
        return bool(visible) and all(value is _IMPORTED_FUNCTOOLS for value, _s in visible)

    def targets(
        self, expr: ast.AST, scope: ast.AST | None, seen: frozenset[int] = frozenset()
    ) -> list[str]:
        """Guarded callables invoked when ``expr`` is called."""
        if id(expr) in seen:
            return []
        seen = seen | {id(expr)}

        direct = _direct_name(expr)
        if direct is not None:
            return [direct]
        if isinstance(expr, ast.Lambda):
            return self._called_within(expr, seen)
        if self.is_partial(expr, scope):
            assert isinstance(expr, ast.Call)
            # Only the callable. Every *other* positional argument is data the
            # partial will pass along, and reading one as the dispatched
            # callable invents a site (#2249 review).
            return self.targets(expr.args[0], scope, seen) if expr.args else []
        if isinstance(expr, ast.Name):
            visible = self.scopes.lookup(expr.id, scope)
            if any(not isinstance(value, ast.AST) for value, _scope in visible):
                # Something binds this name that the guard cannot follow — a
                # comprehension target, an augmented assignment, an import. The
                # alias it may also carry is no longer trustworthy, so the name
                # resolves to nothing: a missed report, never an invented one.
                return []
            out: list[str] = []
            for value, bound_scope in visible:
                if isinstance(value, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    found = self._called_within(value, seen)
                else:
                    found = self.targets(value, bound_scope, seen)
                for name in found:
                    if name not in out:
                        out.append(name)
            return out
        return []

    def _called_within(self, scope: ast.AST, seen: frozenset[int]) -> list[str]:
        """Guarded callables a wrapper's own body calls.

        Its *body*, not everything lexically inside it: a nested function this
        wrapper only defines is not one this wrapper runs (#2249 review).
        """
        out: list[str] = []
        for node in self.scopes._own_nodes(scope):
            if not isinstance(node, ast.Call):
                continue
            for found in self.targets(node.func, scope, seen):
                if found not in out:
                    out.append(found)
        return out


def _thread_dispatch_sites(tree: ast.AST) -> list[tuple[ast.Call, str]]:
    """Calls that hand a guarded callable to a thread, with the name matched.

    Matched by the *argument*, not the callee, so every hand-off spelling is
    found — including the ones no scope can rescue (see
    :func:`_propagates_context`).

    A ``functools.partial`` call is resolved *through* rather than reported as
    a site of its own: it builds a callable, and the hand-off is whatever
    dispatches it. That also fixes the location — the old matcher rejected the
    partial by accident, for not being ``asyncio.to_thread``, and pointed at
    the construction (#2249). ``asyncio.to_thread(partial(engine, root))``
    genuinely does copy the caller's context, so rejecting it outright would be
    a false positive; it is accepted when, and only when, it sits in the scopes
    its callable's rule names.
    """
    matcher = _Matcher(tree)

    sites = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        scope = matcher.scopes.scope_of(node)
        if matcher.is_partial(node, scope):
            continue
        seen_names: set[str] = set()
        for arg in node.args:
            for name in matcher.targets(arg, scope):
                if name not in seen_names:
                    seen_names.add(name)
                    sites.append((node, name))
    return sites


def _propagates_context(site: ast.Call) -> bool:
    """Whether this hand-off carries the caller's ``ContextVar`` state.

    ``asyncio.to_thread`` copies the current context into the worker, which is
    the whole reason a ``ContextVar`` pin survives the hand-off.
    ``loop.run_in_executor`` does **not** — a dispatcher written that way would
    sit inside the ``with`` block and still resolve homes from the live
    environment, so being pinned proves nothing there and the guard must reject
    it rather than wave it through on position alone.
    """
    func = site.func
    # Specifically ``asyncio.to_thread``. Accepting any ``*.to_thread`` would
    # approve an executor wrapper of the same name whose propagation is
    # unknown; requiring the module qualifier keeps the rule to the one API
    # whose contract is documented. A future ``from asyncio import to_thread``
    # would fail here — loudly, which is the right direction for a guard to be
    # wrong in.
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "to_thread"
        and isinstance(func.value, ast.Name)
        and func.value.id == "asyncio"
    )


def _scope_spans(tree: ast.AST, scope: str) -> list[tuple[int, int]]:
    """Line spans of every ``with <scope>(...)`` block.

    Parameterized over the name so both guards read the same ``with``
    statement: ``with pinned_host_homes(), abandon_sync_on_exit():`` is one
    node whose ``items`` carry both, and each lookup finds its own.
    """
    spans = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.With, ast.AsyncWith)):
            continue
        for item in node.items:
            call = item.context_expr
            if not isinstance(call, ast.Call):
                continue
            name = getattr(call.func, "id", None) or getattr(call.func, "attr", None)
            if name == scope:
                spans.append((node.lineno, node.end_lineno or node.lineno))
    return spans


def _python_files() -> list[Path]:
    return sorted(p for p in _SRC.rglob("*.py") if "__pycache__" not in p.parts)


#: Dispatch sites each callable is known to have today. A per-callable floor,
#: not one total: a matcher regression that stopped seeing (say) the CAS writer
#: would still clear a global ``>= 3`` on the settings dispatchers alone.
#: ``apply_migration`` is 0 by design — it has no threaded dispatcher yet.
_MIN_SITES = {
    "generate_all_settings": 3,  # web sync + two MCP context tools
    "apply_hook_copy": 1,  # the web copy route
    "apply_migration": 0,
    "_locked_cas_write": 3,  # resolve / delete / promote
    "generate_all_skills": 3,  # web sync core + two MCP context tools
    "extract_skills_to_canonical": 4,  # three web import routes + one MCP tool
    # Two each: the skills routes and the shared agents/commands routes in
    # ``_atomic_kind``. A floor of 1 would stay green with an entire mirror
    # gone, which is exactly the drift a shared-name closure invites.
    "generate_all_mcp_servers": 1,  # the web sync core, shared with sync-all
    "_create_locked": 2,
    "_update_locked": 2,
    "_delete_locked": 2,
}


def test_the_guard_finds_the_known_dispatchers():
    """A guard that matches nothing would pass forever — pin the lower bound."""
    found: dict[str, int] = dict.fromkeys(_DISPATCH_RULES, 0)
    for path in _python_files():
        for _site, name in _thread_dispatch_sites(ast.parse(path.read_text(encoding="utf-8"))):
            found[name] += 1
    short = {
        name: (found[name], floor) for name, floor in _MIN_SITES.items() if found[name] < floor
    }
    assert not short, (
        f"fewer dispatch sites than expected (found, expected-at-least): {short} — "
        "either the matcher stopped seeing a spelling, or a dispatcher was "
        "removed and this floor should be lowered deliberately"
    )
    assert set(_MIN_SITES) == set(_DISPATCH_RULES), (
        "every guarded callable needs a floor, or a matcher regression on it goes unnoticed"
    )


def test_every_threaded_settings_dispatch_is_inside_its_scopes():
    offenders: list[str] = []
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        sites = _thread_dispatch_sites(tree)
        if not sites:
            continue
        for site, name in sites:
            where = f"{path.relative_to(_SRC)}:{site.lineno} ({name})"
            if not _propagates_context(site):
                offenders.append(f"{where} — hand-off does not copy the caller's context")
                continue
            for scope, issue in _DISPATCH_RULES[name]:
                spans = _scope_spans(tree, scope)
                if not any(start <= site.lineno <= end for start, end in spans):
                    offenders.append(f"{where} — outside `with {scope}():` ({issue})")
    assert not offenders, (
        "settings work handed to a thread without the guards its worker needs:\n  "
        + "\n  ".join(offenders)
        + "\nA worker outlives its caller, so it must carry the abort flag that "
        "stops it writing behind a response that already failed (#2218, #2247) "
        "and — where it resolves host homes itself — the homes the caller "
        "resolved (#2211). Use `asyncio.to_thread` inside the scopes that "
        "callable's rule names, or dispatch through "
        "`contextvars.copy_context().run` explicitly."
    )


def _guard_accepts(source: str) -> bool:
    """Run the guard's own rule over a snippet."""
    tree = ast.parse(source)
    sites = _thread_dispatch_sites(tree)
    assert sites, "fixture should contain a dispatch"
    return all(
        _propagates_context(site)
        and all(
            any(start <= site.lineno <= end for start, end in _scope_spans(tree, scope))
            for scope, _issue in _DISPATCH_RULES[name]
        )
        for site, name in sites
    )


@pytest.mark.parametrize(
    ("label", "source"),
    [
        (
            "unguarded to_thread",
            "import asyncio\nasync def f():\n    await asyncio.to_thread(generate_all_settings, r)\n",
        ),
        (
            "dispatch after the scopes have exited",
            "import asyncio\nasync def f():\n"
            "    with pinned_host_homes(), abandon_sync_on_exit():\n        pass\n"
            "    await asyncio.to_thread(generate_all_settings, r)\n",
        ),
        (
            # Inside the scopes, yet still broken: run_in_executor does not
            # copy the context, so neither ContextVar reaches the worker.
            "guarded run_in_executor",
            "import asyncio\nasync def f():\n"
            "    with pinned_host_homes(), abandon_sync_on_exit():\n"
            "        await loop.run_in_executor(None, generate_all_settings, r)\n",
        ),
        (
            # A qualified reference is the same dispatch one import hop away.
            "unguarded qualified dispatch",
            "import asyncio\nasync def f():\n"
            "    await asyncio.to_thread(settings.generate_all_settings, r)\n",
        ),
        (
            # Same method name, different object: propagation is not asyncio's
            # to promise.
            "guarded executor.to_thread",
            "import asyncio\nasync def f():\n"
            "    with pinned_host_homes(), abandon_sync_on_exit():\n"
            "        await executor.to_thread(generate_all_settings, r)\n",
        ),
        (
            # The half-guarded shape the #2218 half of the rule exists for:
            # the write goes to the right home and still should not happen.
            "pinned but not abandon-scoped",
            "import asyncio\nasync def f():\n"
            "    with pinned_host_homes():\n"
            "        await asyncio.to_thread(generate_all_settings, r)\n",
        ),
        (
            # And the mirror: cancellable, but free to follow $HOME.
            "abandon-scoped but not pinned",
            "import asyncio\nasync def f():\n"
            "    with abandon_sync_on_exit():\n"
            "        await asyncio.to_thread(generate_all_settings, r)\n",
        ),
        (
            # A sibling engine owes the abort flag even though it owes no pin
            # — the half the per-callable table must not turn into "anything
            # goes for the new entries" (#2247).
            "sibling engine outside the abort scope",
            "import asyncio\nasync def f():\n    await asyncio.to_thread(apply_hook_copy, plan)\n",
        ),
        (
            # Pinning a sibling engine does not substitute for the flag: the
            # pin answers *where*, and nothing here answers *whether*.
            "sibling engine pinned instead of abort-scoped",
            "import asyncio\nasync def f():\n"
            "    with pinned_host_homes():\n"
            "        await asyncio.to_thread(_locked_cas_write, p, m, doc)\n",
        ),
        (
            # #2249: one assignment away from invisible. The worker gets the
            # engine either way, so an alias must not launder the reference.
            "unguarded alias",
            "import asyncio\nrun = generate_all_settings\nasync def f():\n"
            "    await asyncio.to_thread(run, r)\n",
        ),
        (
            # #2249: the name is in the lambda's body, not in the call's args.
            "unguarded lambda wrapper",
            "import asyncio\nasync def f():\n"
            "    await asyncio.to_thread(lambda: generate_all_settings(r, scope=s))\n",
        ),
        (
            # #2249: the lambda case with a name on it.
            "unguarded nested def wrapper",
            "import asyncio\nasync def f():\n"
            "    def _run():\n        generate_all_settings(r)\n"
            "    await asyncio.to_thread(_run)\n",
        ),
        (
            # #2249: caught before, but only by accident — the partial call was
            # itself read as a hand-off and rejected for not being to_thread.
            "unguarded functools.partial",
            "import asyncio\nimport functools\nasync def f():\n"
            "    await asyncio.to_thread(functools.partial(generate_all_settings, r))\n",
        ),
        (
            # A partial is transparent in both directions: an executor still
            # loses the context, whatever the callable was built from.
            "guarded partial through run_in_executor",
            "import asyncio\nfrom functools import partial\nasync def f():\n"
            "    with pinned_host_homes(), abandon_sync_on_exit():\n"
            "        await loop.run_in_executor(None, partial(generate_all_settings, r))\n",
        ),
        (
            # An alias resolves transitively, or one more hop hides it again.
            "unguarded alias of an alias",
            "import asyncio\nfirst = generate_all_settings\nsecond = first\n"
            "async def f():\n    await asyncio.to_thread(second, r)\n",
        ),
        (
            # Storing the wrapper first is the same dispatch with a name on it,
            # and was the remaining hole after the first cut (#2249 review).
            "unguarded stored lambda",
            "import asyncio\nasync def f():\n"
            "    job = lambda: generate_all_settings(r)\n"
            "    await asyncio.to_thread(job)\n",
        ),
        (
            "unguarded stored partial",
            "import asyncio\nfrom functools import partial\nasync def f():\n"
            "    job = partial(generate_all_settings, r)\n"
            "    await asyncio.to_thread(job)\n",
        ),
        (
            "unguarded aliased nested def",
            "import asyncio\nasync def f():\n"
            "    def inner():\n        generate_all_settings(r)\n"
            "    job = inner\n    await asyncio.to_thread(job)\n",
        ),
        (
            # A *local* callable named ``partial`` is not the stdlib one, and
            # this one dispatches without copying the context — the exemption
            # is proven from the imports, not from the spelling (#2249 review).
            "local partial that loses the context",
            "def partial(fn, *args):\n    dispatch_without_context(fn, *args)\n"
            "partial(generate_all_settings, r)\n",
        ),
        (
            # Even when the module *does* import the stdlib one: the binding
            # visible at the call is the local shadow (#2249 review).
            "imported partial shadowed by a local one",
            "from functools import partial\ndef partial(fn, *args):\n"
            "    dispatch_without_context(fn, *args)\n"
            "partial(generate_all_settings, r)\n",
        ),
    ],
)
def test_guard_rejects_shapes_that_lose_a_guard(label, source):
    """The guard must fail on the shapes it claims to catch, not just pass today."""
    assert not _guard_accepts(source), f"guard would have accepted: {label}"


@pytest.mark.parametrize(
    "source",
    [
        "import asyncio\nasync def f():\n"
        "    with pinned_host_homes(), abandon_sync_on_exit():\n"
        "        await asyncio.to_thread(generate_all_settings, r)\n",
        # The qualified spelling is fine too, as long as it is guarded and
        # dispatched through asyncio.
        "import asyncio\nasync def f():\n"
        "    with pinned_host_homes(), abandon_sync_on_exit():\n"
        "        await asyncio.to_thread(settings.generate_all_settings, r)\n",
        # Nested rather than stacked: two `with` statements express the same
        # two spans, and the rule is about the spans, not the spelling.
        "import asyncio\nasync def f():\n"
        "    with pinned_host_homes():\n"
        "        with abandon_sync_on_exit():\n"
        "            await asyncio.to_thread(generate_all_settings, r)\n",
        # A sibling engine needs the abort flag and NOT the pin: it writes
        # paths its caller resolved. Demanding a pin here would be a rule
        # applied past its rationale, so the guard must accept this (#2247).
        "import asyncio\nasync def f():\n"
        "    with abandon_sync_on_exit():\n"
        "        await asyncio.to_thread(apply_hook_copy, plan)\n",
        # #2249, the other direction for each new spelling: a wider matcher
        # that rejected these would be a bigger surface to be wrong on, not a
        # better guard. Each of these dispatches correctly.
        "import asyncio\nrun = generate_all_settings\nasync def f():\n"
        "    with pinned_host_homes(), abandon_sync_on_exit():\n"
        "        await asyncio.to_thread(run, r)\n",
        "import asyncio\nasync def f():\n"
        "    with pinned_host_homes(), abandon_sync_on_exit():\n"
        "        await asyncio.to_thread(lambda: generate_all_settings(r))\n",
        "import asyncio\nasync def f():\n"
        "    def _run():\n        generate_all_settings(r)\n"
        "    with pinned_host_homes(), abandon_sync_on_exit():\n"
        "        await asyncio.to_thread(_run)\n",
        # `partial` copies nothing and hides nothing: `asyncio.to_thread` still
        # carries the context, so a guarded partial dispatch is correct and the
        # guard must say so rather than rejecting it for the wrong reason.
        "import asyncio\nfrom functools import partial\nasync def f():\n"
        "    with pinned_host_homes(), abandon_sync_on_exit():\n"
        "        await asyncio.to_thread(partial(generate_all_settings, r))\n",
        # The stored spellings, guarded. A matcher that only learned to reject
        # more would be a bigger surface to be wrong on, not a better guard.
        "import asyncio\nasync def f():\n"
        "    job = lambda: generate_all_settings(r)\n"
        "    with pinned_host_homes(), abandon_sync_on_exit():\n"
        "        await asyncio.to_thread(job)\n",
        "import asyncio\nfrom functools import partial\nasync def f():\n"
        "    job = partial(generate_all_settings, r)\n"
        "    with pinned_host_homes(), abandon_sync_on_exit():\n"
        "        await asyncio.to_thread(job)\n",
    ],
)
def test_guard_accepts_the_correct_shape(source):
    """Rejecting everything would also pass the tests above — pin the positive."""
    assert _guard_accepts(source)


@pytest.mark.parametrize(
    ("label", "source", "expected"),
    [
        (
            # The same local name in two functions. A module-wide table would
            # read ``register(run)`` as a dispatch of the engine bound in the
            # *other* function (#2249 review).
            "an innocent call under a name bound elsewhere",
            "def a():\n    run = harmless\n    register(run)\n"
            "def b():\n    run = generate_all_settings\n",
            [],
        ),
        (
            # ...and the mirror: shadowing must not hide the real one either.
            "the real dispatch survives a shadowing binding",
            "import asyncio\nasync def a():\n    run = generate_all_settings\n"
            "    await asyncio.to_thread(run, r)\n"
            "async def b():\n    run = harmless\n"
            "    await asyncio.to_thread(run, r)\n",
            ["generate_all_settings"],
        ),
        (
            # Defining a nested function is not calling it, so the wrapper this
            # dispatches runs nothing guarded (#2249 review).
            "a wrapper that only defines a guarded call",
            "import asyncio\ndef outer():\n    def unused():\n"
            "        generate_all_settings(r)\n    return 1\n"
            "async def f():\n    await asyncio.to_thread(outer)\n",
            [],
        ),
        (
            # A parameter is a binding: the outer alias must not shine through
            # it (#2249 review).
            "a parameter shadows an outer alias",
            "import asyncio\nrun = generate_all_settings\n"
            "async def f(run):\n    await asyncio.to_thread(run, r)\n",
            [],
        ),
        (
            # ...and the unpacked mirror, which is a real hand-off.
            "an unpacked alias is still an alias",
            "import asyncio\nasync def f():\n"
            "    run, other = generate_all_settings, harmless\n"
            "    await asyncio.to_thread(run, r)\n",
            ["generate_all_settings"],
        ),
        (
            # A comprehension target rebinds the name for that expression.
            "a comprehension target is not the outer alias",
            "run = generate_all_settings\n"
            "[dispatch_without_context(run) for run in harmless_callables]\n",
            [],
        ),
        (
            # A class body is its own namespace and does not overwrite the
            # module name a function later resolves (#2249 review).
            "a class attribute does not hide a module-level dispatch",
            "import asyncio\nrun = generate_all_settings\nclass C:\n    run = harmless\n"
            "async def f():\n    await asyncio.to_thread(run, r)\n",
            ["generate_all_settings"],
        ),
        (
            # An attribute's receiver is not modelled, so its bare tail must
            # not be resolved through the local name table (#2249 review).
            "an attribute is not a local of the same name",
            "import asyncio\nrun = generate_all_settings\n"
            "async def f():\n    await asyncio.to_thread(worker.run)\n",
            [],
        ),
        (
            # Same name on two classes: neither is resolvable, and guessing
            # would invent a dispatch as readily as miss one.
            "same-named methods do not collide",
            "import asyncio\nclass A:\n    def job(self):\n        harmless()\n"
            "class B:\n    def job(self):\n        generate_all_settings(r)\n"
            "async def f():\n    await asyncio.to_thread(A().job)\n",
            [],
        ),
        (
            # A decorator runs in the ENCLOSING scope, so a body-local
            # rebinding cannot invent one (#2249 review)...
            "a decorator reads the enclosing scope",
            "run = harmless\n@register(run)\ndef f():\n    run = generate_all_settings\n",
            [],
        ),
        (
            # ...nor hide one: a decorator that dispatches is a dispatch.
            "a decorator that hands off is still a hand-off",
            "run = generate_all_settings\n@dispatch_without_context(run)\ndef f():\n    pass\n",
            ["generate_all_settings"],
        ),
        (
            # ``partial``'s later positional arguments are data, not the
            # callable it will invoke (#2249 review).
            "a guarded name passed as partial data is not dispatched",
            "import asyncio\nfrom functools import partial\nasync def f():\n"
            "    await asyncio.to_thread(partial(harmless, generate_all_settings))\n",
            [],
        ),
        (
            # An augmented assignment leaves the name somewhere this guard
            # cannot follow, and an unfollowable name resolves to nothing.
            "an augmented assignment stops the alias",
            "run = generate_all_settings\nrun += harmless\ndispatch_without_context(run)\n",
            [],
        ),
    ],
)
def test_the_matcher_reads_bindings_per_scope(label, source, expected):
    """Resolving indirections must not invent dispatches that are not there.

    A guard that flags an innocent call is not a stricter guard — it is one the
    next contributor learns to work around.
    """
    sites = _thread_dispatch_sites(ast.parse(source))
    assert [name for _site, name in sites] == expected, label


@pytest.mark.parametrize(
    ("label", "source"),
    [
        (
            "alias",
            "import asyncio\nrun = generate_all_settings\nasync def f():\n"
            "    await asyncio.to_thread(run, r)\n",
        ),
        (
            "lambda",
            "import asyncio\nasync def f():\n"
            "    await asyncio.to_thread(lambda: generate_all_settings(r))\n",
        ),
        (
            "nested def",
            "import asyncio\nasync def f():\n"
            "    def _run():\n        generate_all_settings(r)\n"
            "    await asyncio.to_thread(_run)\n",
        ),
        (
            "partial",
            "import asyncio\nfrom functools import partial\nasync def f():\n"
            "    await asyncio.to_thread(partial(generate_all_settings, r))\n",
        ),
    ],
)
def test_the_offender_line_names_the_dispatch_not_the_indirection(label, source):
    """An offender a reader cannot find is a guard that reports nothing useful.

    Every one of these resolves to a single site, and that site is the
    ``asyncio.to_thread`` call — not the alias assignment, not the ``partial``
    construction (which is what the old accidental rejection pointed at) and
    not the wrapper's own body (#2249).
    """
    tree = ast.parse(source)
    sites = _thread_dispatch_sites(tree)
    assert len(sites) == 1, f"{label}: expected one site, got {[n for _s, n in sites]}"
    site, name = sites[0]
    assert name == "generate_all_settings"
    assert _reference_name(site.func) == "to_thread", (
        f"{label}: the site is {ast.dump(site.func)}, not the hand-off"
    )
