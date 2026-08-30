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
whose rationale does not reach them. The skills engines are the awkward case:
they *do* resolve user homes in the worker, so they will owe the pin, but
there is nothing for them to carry yet — the resolvers ignore it — and that
gap is tracked as its own #2211-shaped bug rather than silently implied by
this table. Hence :data:`_DISPATCH_RULES` rather than one list of scopes.

A callable's presence in that table is itself a claim: that its engine has
abort checks placed against its own transaction boundaries, so handing it the
flag means something.

Per-site regression tests cannot cover a dispatcher that does not exist yet, so
the rule is enforced lexically over the tree instead: the guard finds the call
sites itself rather than reading a hand-kept list of *sites* that a new
dispatcher would silently sit outside of.
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
#: ``generate_all_settings`` is the only entry that owes the pin today: it
#: resolves ``$HOME``-anchored targets inside the worker AND reads the pin. See
#: the module docstring for why the skills entries are abort-only despite
#: resolving homes in the worker too.
_DISPATCH_RULES: dict[str, tuple[tuple[str, str], ...]] = {
    "generate_all_settings": (_PIN, _ABANDON),
    "apply_hook_copy": (_ABANDON,),
    # No threaded dispatcher today (CLI only). Listed so the first one that
    # appears fails here until it is wrapped, rather than shipping a pair-lock
    # transaction that can write behind a 503 (#2247).
    "apply_migration": (_ABANDON,),
    "_locked_cas_write": (_ABANDON,),
    # Skills engines and their CRUD closures (#2247). Abort-only for now, but
    # for a different reason than the settings siblings: these DO resolve
    # user-scope homes inside the worker (`scope_resolver` / `_runtime_targets`
    # call `expanduser()` there), so they have the #2211 shape and no pin to
    # carry yet. Tracked as its own issue; when it lands, `_PIN` belongs on
    # both engine entries here.
    "generate_all_skills": (_ABANDON,),
    "extract_skills_to_canonical": (_ABANDON,),
    # One target, one write, and ``.mcp.json`` is project-rooted from an
    # argument — abort-only, with nothing home-anchored to pin (#2247).
    "generate_all_mcp_servers": (_ABANDON,),
    "_create_locked": (_ABANDON,),
    "_update_locked": (_ABANDON,),
    "_delete_locked": (_ABANDON,),
}


def _thread_dispatch_sites(tree: ast.AST) -> list[tuple[ast.Call, str]]:
    """Calls that hand a guarded callable to a thread, with the name matched.

    Matched by the *argument*, not the callee, so every hand-off spelling is
    found — including the ones no scope can rescue (see
    :func:`_propagates_context`).
    """
    sites = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for arg in node.args:
            # Both spellings: the bare name, and a qualified reference such as
            # ``settings.generate_all_settings``. Matching only the bare name
            # would let one import-style hop carry a dispatcher out of view.
            name = arg.id if isinstance(arg, ast.Name) else getattr(arg, "attr", None)
            if name in _DISPATCH_RULES:
                sites.append((node, name))
                break
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
    ],
)
def test_guard_accepts_the_correct_shape(source):
    """Rejecting everything would also pass the tests above — pin the positive."""
    assert _guard_accepts(source)
